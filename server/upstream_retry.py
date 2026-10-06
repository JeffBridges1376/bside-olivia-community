"""Retry explicit pre-output failures inside a single relay billing record."""
from contextlib import asynccontextmanager
import asyncio
import json

import httpx

TRANSIENT = {429, 500, 502, 503, 504, 529}
TERMINAL = {'insufficient_quota', 'insufficient_balance', 'quota_exceeded',
    'credit_balance_too_low', 'invalid_api_key', 'authentication_error',
    'permission_error', 'invalid_request_error', 'context_length_exceeded',
    'billing_error', 'model_not_found'}
UNKNOWN = {'upstream_unavailable_usage_pending', 'usage_unavailable',
    'usage_reconciliation_required', 'request_already_submitted',
    'upstream_timeout', 'request_timeout', 'gateway_timeout', 'response_timeout', 'read_timeout'}
TOKEN_FIELDS = ('prompt_tokens', 'completion_tokens', 'input_tokens', 'output_tokens', 'total_tokens')


def failure(response):
    """A structured error without generated content is an explicit rejection."""
    try:
        if len(response.content) > 16384:
            return False, False
        value = response.json()
        if not isinstance(value, dict) or not isinstance(value.get('error'), dict) or not value['error']:
            return False, False
        if response.status_code < 400 and value.get('status') not in (None, 'failed'):
            return False, False
        if any(value.get(k) for k in ('choices', 'output', 'output_text', 'content', 'text')):
            return False, False
        usage = value.get('usage')
        if usage is not None:
            if not isinstance(usage, dict) or not any(k in usage for k in TOKEN_FIELDS):
                return False, False
            if any(type(usage[k]) is not int or usage[k] != 0 for k in TOKEN_FIELDS if k in usage):
                return False, False
        error = value['error']
        codes = {str(error.get(k, '')).lower() for k in ('type', 'code', 'status')}
        if codes & UNKNOWN:
            return False, False
        retry = (response.status_code in TRANSIENT or (response.status_code < 400 and
                 codes & {'server_error', 'overloaded_error', 'rate_limit_exceeded'}))
        return True, bool(retry and not codes & TERMINAL and error.get('retryable') is not False)
    except (ValueError, TypeError):
        return False, False


class RetryClient:
    def __init__(self, client, trace, revalidate=None):
        self.client, self.trace, self.revalidate = client, trace, revalidate

    def begin(self, attempt):
        self.trace.update(upstream_attempts=attempt + 1, upstream_failure_confirmed=False)

    def retry(self, response, attempt):
        confirmed, retry = failure(response)
        self.trace['upstream_failure_confirmed'] = confirmed
        return retry and attempt < 2

    async def wait(self, response, attempt):
        delay = 5 if response.status_code == 429 else .5 * (attempt + 1)
        try:
            delay = min(60, max(delay, int(response.headers.get('retry-after', '0'))))
        except ValueError:
            pass
        await asyncio.sleep(delay)

    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        for attempt in range(3):
            if attempt and self.revalidate is not None:
                await self.revalidate()
            self.begin(attempt)
            async with self.client.stream(*args, **kwargs) as response:
                # Responses is non-streaming upstream even when the client wants
                # SSE. Buffer only JSON paths that already require a full body.
                inspect_json = (str(args[1]).endswith('/responses') or kwargs.get('json', {}).get('stream') is False
                    or response.headers.get('content-type', '').split(';')[0].strip().lower() == 'application/json')
                if response.status_code >= 400 or inspect_json:
                    limit = 16384 if response.status_code >= 400 else 4 * 1024 * 1024
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > limit:
                            if response.status_code < 400:
                                raise ValueError('Oversized response')
                            body = bytearray()
                            break
                    response = httpx.Response(response.status_code, headers=response.headers,
                                              content=bytes(body), request=response.request)
                if (response.status_code < 400 and not inspect_json) or not self.retry(response, attempt):
                    if response.status_code < 400 and self.trace['upstream_failure_confirmed']:
                        raise ValueError('Explicit upstream failure')
                    yield response
                    return
            await self.wait(response, attempt)

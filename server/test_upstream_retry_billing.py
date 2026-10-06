"""Staged live Relay with an isolated Django test database and synthetic upstream."""
import json
import uuid
import asyncio
from datetime import timedelta
from unittest.mock import patch

import httpx
from asgiref.testing import ApplicationCommunicator
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from qwen.async_relay import Relay, db
from qwen.models import AccessKey, Usage
from qwen.quota import issue_key, grant_money
from qwen.relay_models import public_models


async def no_wait(*args):
    pass


@override_settings(RELAYJETTY_API_KEY='synthetic-provider', RELAYJETTY_BASE_URL='https://synthetic.invalid/v1',
                   QWEN_API_KEY='synthetic-provider', QWEN_BASE_URL='https://synthetic.invalid/v1')
class RetryBillingTests(TransactionTestCase):
    def setUp(self):
        self.account, self.key = issue_key('SYNTHETIC cloud retry', billing_mode='money')
        grant_money(self.account.pk, 1_000_000_000, 'synthetic-retry-credit', 'test')

    def success(self, request):
        data = json.loads(request.content)
        if request.url.path.endswith('/responses'):
            return httpx.Response(200, json={'id': 'synthetic-response', 'status': 'completed',
                'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'synthetic'}]}],
                'usage': {'input_tokens': 5, 'output_tokens': 2, 'total_tokens': 7}})
        value = {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'synthetic'},
                             'finish_reason': 'stop'}],
                 'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7}}
        if data.get('stream'):
            value['choices'][0]['delta'] = value['choices'][0].pop('message')
            return httpx.Response(200, content=('data: ' + json.dumps(value) + '\n\ndata: [DONE]\n\n').encode())
        return httpx.Response(200, json=value)

    async def submit(self, relay, model, *, stream=False, identity=None):
        communicator = ApplicationCommunicator(relay, {'type': 'http', 'method': 'POST',
            'path': '/v1/chat/completions', 'headers': [
                (b'authorization', ('Bearer ' + self.key).encode()),
                (b'idempotency-key', (identity or uuid.uuid4().hex).encode())]})
        payload = {'model': model, 'messages': [{'role': 'user', 'content': 'synthetic'}],
                   'max_tokens': 16, 'stream': stream}
        await communicator.send_input({'type': 'http.request', 'body': json.dumps(payload).encode()})
        start = await communicator.receive_output(timeout=5)
        body = b''
        while True:
            event = await communicator.receive_output(timeout=5)
            body += event.get('body', b'')
            if not event.get('more_body'):
                break
        await communicator.wait(timeout=5)
        return start['status'], body

    async def test_all_models_stream_and_nonstream_retry_with_one_settlement(self):
        expected = 0
        for item in await db(public_models):
            for stream in (False, True):
                calls = []
                def handler(request):
                    calls.append(request.content)
                    if len(calls) == 1:
                        return httpx.Response(503, json={'error': {'type': 'overloaded_error'}})
                    return self.success(request)
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    relay = Relay(None, client=client)
                    with patch('qwen.upstream_retry.RetryClient.wait', no_wait):
                        status, body = await self.submit(relay, item['id'], stream=stream)
                self.assertEqual(status, 200, body)
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0], calls[1])
                self.assertEqual(relay.live, 0)
                expected += 1
                # Simulated turns are separated in time so the real 10/minute
                # admission rule remains intact rather than being mocked out.
                await db(Usage.objects.filter(account=self.account).update,
                         created_at=timezone.now() - timedelta(minutes=2))
        rows = await db(lambda: list(Usage.objects.filter(account=self.account)))
        self.assertEqual(len(rows), expected)
        self.assertTrue(all(r.status == 'settled' and r.prompt_tokens == 5 and r.completion_tokens == 2 for r in rows))
        account = await db(AccessKey.objects.get, pk=self.account.pk)
        self.assertEqual(account.used_units, sum(r.charged_units for r in rows))
        self.assertEqual(account.held_units, 0)

    async def test_exhausted_errors_cost_zero_and_do_not_repeat_on_duplicate(self):
        for model in ('claude-sonnet-5-5', 'gpt-6.1-sol'):
            calls = []
            def handler(request):
                calls.append(1)
                return httpx.Response(503, json={'error': {'type': 'overloaded_error'}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                relay = Relay(None, client=client)
                identity = uuid.uuid4().hex
                with patch('qwen.upstream_retry.RetryClient.wait', no_wait):
                    status, body = await self.submit(relay, model, identity=identity)
                    duplicate, _ = await self.submit(relay, model, identity=identity)
            self.assertEqual((status, duplicate, len(calls)), (502, 409, 3))
            self.assertEqual(json.loads(body)['error']['code'], 'upstream_rejected')
            self.assertFalse(json.loads(body)['error']['retryable'])
        rows = await db(lambda: list(Usage.objects.filter(account=self.account)))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.status == 'rejected' and r.charged_units == 0 for r in rows))
        account = await db(AccessKey.objects.get, pk=self.account.pk)
        self.assertEqual((account.used_units, account.held_units), (0, 0))

    async def test_read_failure_after_retry_keeps_unknown_usage_without_third_dispatch(self):
        calls = []
        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(503, json={'error': {'type': 'overloaded_error'}})
            raise httpx.ReadTimeout('synthetic')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            relay = Relay(None, client=client)
            with patch('qwen.upstream_retry.RetryClient.wait', no_wait):
                status, body = await self.submit(relay, 'claude-sonnet-5-5')
        self.assertEqual((status, len(calls)), (502, 2))
        self.assertEqual(json.loads(body)['error']['code'], 'upstream_unavailable_usage_pending')
        row = await db(Usage.objects.get, account=self.account)
        self.assertEqual(row.status, 'review')
        self.assertIsNone(row.prompt_tokens)

    async def test_responses_failed_200_retries_before_generation_is_delivered(self):
        calls = []
        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(200, json={'status': 'failed', 'error': {'code': 'server_error'},
                    'output': [], 'usage': {'input_tokens': 0, 'output_tokens': 0}})
            return self.success(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            relay = Relay(None, client=client)
            with patch('qwen.upstream_retry.RetryClient.wait', no_wait):
                status, body = await self.submit(relay, 'gpt-6.1-sol')
        self.assertEqual((status, len(calls)), (200, 2))
        row = await db(Usage.objects.get, account=self.account)
        self.assertEqual((row.status, row.prompt_tokens, row.completion_tokens), ('settled', 5, 2))

    async def test_exhausted_chat_json_errors_do_not_start_a_success_stream(self):
        calls = []
        def handler(request):
            calls.append(1)
            return httpx.Response(200, json={'error': {'code': 'server_error'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            relay = Relay(None, client=client)
            with patch('qwen.upstream_retry.RetryClient.wait', no_wait):
                status, body = await self.submit(relay, 'claude-sonnet-5-5', stream=True)
        self.assertEqual((status, len(calls)), (502, 3))
        self.assertEqual(json.loads(body)['error']['code'], 'upstream_rejected')
        row = await db(Usage.objects.get, account=self.account)
        self.assertEqual((row.status, row.charged_units), ('rejected', 0))

    async def test_revoked_key_stops_retry_and_releases_failed_call(self):
        calls = []
        async def handler(request):
            calls.append(1)
            await db(AccessKey.objects.filter(pk=self.account.pk).update, enabled=False)
            return httpx.Response(503, json={'error': {'type': 'overloaded_error'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            relay = Relay(None, client=client)
            with patch('qwen.upstream_retry.RetryClient.wait', no_wait):
                status, _ = await self.submit(relay, 'claude-sonnet-5-5')
        self.assertEqual((status, len(calls)), (401, 1))
        row = await db(Usage.objects.get, account=self.account)
        self.assertEqual((row.status, row.charged_units), ('rejected', 0))

    async def test_disconnect_between_failed_attempts_releases_zero_cost_call(self):
        waiting = asyncio.Event()
        calls = []
        async def backoff(*args):
            waiting.set()
            await asyncio.Event().wait()
        def handler(request):
            calls.append(1)
            return httpx.Response(503, json={'error': {'type': 'overloaded_error'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            relay = Relay(None, client=client)
            communicator = ApplicationCommunicator(relay, {'type': 'http', 'method': 'POST',
                'path': '/v1/chat/completions', 'headers': [(b'authorization', ('Bearer ' + self.key).encode())]})
            payload = {'model': 'claude-sonnet-5-5', 'messages': [{'role': 'user', 'content': 'synthetic'}], 'max_tokens': 16}
            with patch('qwen.upstream_retry.RetryClient.wait', backoff):
                await communicator.send_input({'type': 'http.request', 'body': json.dumps(payload).encode()})
                await asyncio.wait_for(waiting.wait(), 5)
                await communicator.send_input({'type': 'http.disconnect'})
                await communicator.wait(timeout=5)
        row = await db(Usage.objects.get, account=self.account)
        self.assertEqual((len(calls), row.status, row.charged_units, relay.live), (1, 'rejected', 0, 0))

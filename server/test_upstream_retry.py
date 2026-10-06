import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from upstream_retry import RetryClient


class UpstreamRetryTests(unittest.TestCase):
    def exercise(self, responses, *, post=False):
        calls, delays, trace = [], [], {}
        async def handler(request):
            calls.append(request)
            value = responses[min(len(calls) - 1, len(responses) - 1)]
            if isinstance(value, Exception):
                raise value
            status, body = value
            return httpx.Response(status, json=body)
        async def wait(delay): delays.append(delay)
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                retried = RetryClient(client, trace)
                args = dict(json={'model': 'synthetic-model', 'messages': [{'role': 'user', 'content': 'synthetic'}]},
                            headers={'Authorization': 'Bearer synthetic-private'})
                with patch('asyncio.sleep', wait):
                    url = 'https://synthetic.invalid/v1/' + ('responses' if post else 'chat/completions')
                    async with retried.stream('POST', url, **args) as result:
                        await result.aread()
                        return result.status_code
        status = asyncio.run(run())
        return status, calls, delays, trace

    def test_clear_transient_error_then_success_reuses_exact_request(self):
        for post in (False, True):
            for status in (429, 500, 502, 503, 504, 529):
                with self.subTest(post=post, status=status):
                    actual, calls, delays, trace = self.exercise([
                        (status, {'error': {'type': 'overloaded_error'}}),
                        (200, {'choices': [{'message': {'content': 'synthetic'}}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 2}})], post=post)
                    self.assertEqual(actual, 200)
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(calls[0].content, calls[1].content)
                    self.assertEqual(calls[0].headers, calls[1].headers)
                    self.assertEqual(len(delays), 1)
                    self.assertFalse(trace['upstream_failure_confirmed'])

    def test_exhaustion_stops_after_three_and_retains_confirmed_failure(self):
        for post in (False, True):
            actual, calls, delays, trace = self.exercise([(503, {'error': {'type': 'overloaded_error'}})], post=post)
            self.assertEqual((actual, len(calls), len(delays)), (503, 3, 2))
            self.assertTrue(trace['upstream_failure_confirmed'])

    def test_responses_explicit_failed_without_output_can_retry(self):
        actual, calls, _, _ = self.exercise([
            (200, {'status': 'failed', 'error': {'code': 'server_error'}, 'usage': {'input_tokens': 0, 'output_tokens': 0}}),
            (200, {'status': 'completed', 'output_text': 'synthetic'})], post=True)
        self.assertEqual((actual, len(calls)), (200, 2))

    def test_chat_json_error_with_http_200_can_retry_before_output(self):
        actual, calls, _, _ = self.exercise([
            (200, {'error': {'code': 'server_error'}}),
            (200, {'choices': [{'message': {'content': 'synthetic'}}]})])
        self.assertEqual((actual, len(calls)), (200, 2))

    def test_terminal_or_uncertain_responses_do_not_retry(self):
        examples = [(400, {'error': {'type': 'invalid_request_error'}}),
            (401, {'error': {'type': 'authentication_error'}}),
            (400, {'error': {'type': 'overloaded_error'}}),
            (429, {'error': {'code': 'insufficient_balance'}}),
            (502, {'error': {'code': 'upstream_unavailable_usage_pending'}}),
            (504, {'error': {'code': 'gateway_timeout'}}),
            (503, {}),
            (503, {'error': {'type': 'overloaded_error'}, 'usage': {'prompt_tokens': 1}}),
            (503, {'error': {'type': 'overloaded_error'}, 'choices': [{'message': {'content': 'partial'}}]}),
            (503, {'error': {'type': 'overloaded_error', 'retryable': False}}),
            (200, {'status': 'incomplete', 'error': {'type': 'server_error'}})]
        for post in (False, True):
            for response in examples:
                with self.subTest(post=post, response=response):
                    _, calls, delays, _ = self.exercise([response], post=post)
                    self.assertEqual((len(calls), delays), (1, []))

    def test_transport_failure_after_clear_rejection_is_not_retried(self):
        calls = []
        async def run():
            async def handler(request):
                calls.append(request)
                if len(calls) == 1:
                    return httpx.Response(503, json={'error': {'type': 'overloaded_error'}})
                raise httpx.ReadTimeout('synthetic')
            async def wait(delay): pass
            trace = {}
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch('asyncio.sleep', wait):
                    with self.assertRaises(httpx.ReadTimeout):
                        async with RetryClient(client, trace).stream('POST', 'https://synthetic.invalid/responses'):
                            pass
            self.assertFalse(trace['upstream_failure_confirmed'])
        asyncio.run(run())
        self.assertEqual(len(calls), 2)

    def test_read_failure_after_stream_output_never_repeats_generation(self):
        calls = []
        class Partial(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"content":"synthetic partial"}\n\n'
                raise httpx.ReadError('synthetic interrupted stream')
        async def run():
            def handler(request):
                calls.append(request)
                return httpx.Response(200, stream=Partial(), headers={'content-type': 'text/event-stream'})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(httpx.ReadError):
                    async with RetryClient(client, {}).stream('POST', 'https://synthetic.invalid/chat/completions') as result:
                        await result.aread()
        asyncio.run(run())
        self.assertEqual(len(calls), 1)

    def test_retry_after_and_backoff_are_bounded(self):
        delays = []
        async def wait(delay): delays.append(delay)
        async def run():
            retried = RetryClient(None, {})
            with patch('asyncio.sleep', wait):
                for status, header in ((503, '999999'), (503, '-2'), (503, 'invalid'), (429, '0')):
                    await retried.wait(httpx.Response(status, headers={'retry-after': header}), 0)
        asyncio.run(run())
        self.assertEqual(delays, [60, .5, .5, 5])


if __name__ == '__main__':
    unittest.main()

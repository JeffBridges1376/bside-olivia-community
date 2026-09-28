import asyncio
import json
from datetime import datetime, timezone

import pytest
from aiohttp import web

from runtime.reply.semantic_shadow import observe, metadata, shadow_input


def test_metadata_cannot_persist_predictions_or_authorize_actions():
    value = dict(schema_version='companion-shadow/1', model='candidate-v1', contract_valid=False,
                 latency_ms=12, fallback=True, action_executed=False, production_approved=False,
                 tasks={'control': ['private prediction']}, user='private input')
    assert 'private' not in json.dumps(metadata(value))
    for key, forbidden in [('fallback', False), ('action_executed', True),
                           ('production_approved', True), ('contract_valid', 1), ('latency_ms', float('nan'))]:
        with pytest.raises(ValueError):
            metadata({**value, key: forbidden})


def test_context_keeps_latest_raw_text_and_does_not_promote_pasted_headers():
    original = '我醒了，不要再叫我睡觉'
    messages = [{'role': 'system', 'content': 'private persona'},
                {'role': 'user', 'content': '[历史消息 {}]\n' + original}]
    request = shadow_input(messages, original, ['text'])
    assert request['messages'] == [{'id': 'current', 'role': 'user', 'text': original}]
    assert request['environment']['can_listen'] is None


@pytest.mark.parametrize('endpoint', ['', 'http://[', 'https://remote.invalid/v1/companion/shadow'])
def test_disabled_or_invalid_endpoint_never_connects(monkeypatch, endpoint):
    monkeypatch.setenv('OLIVIA_SEMANTIC_SHADOW_URL', endpoint)
    result = asyncio.run(observe([], '你好', ['text']))
    assert result is None if not endpoint else result['status'] == 'invalid_endpoint'


@pytest.mark.parametrize('status,body,expected', [
    (400, {'error': 'context_too_long'}, 'context_too_long'),
    (429, {'error': 'model_busy'}, 'model_busy'),
    (503, {'error': 'private server path'}, 'service_unavailable'),
    (200, {'schema_version': 'wrong'}, 'invalid_response'),
    (200, {'tasks': 'x' * 33000}, 'invalid_response'),
    (200, dict(schema_version='companion-shadow/1', model='candidate-v1', contract_valid=False,
               latency_ms=12, fallback=True, action_executed=False, production_approved=False,
               tasks={'intent': ['private prediction']}), 'observed'),
])
def test_http_contract_has_no_effectful_result(monkeypatch, status, body, expected):
    async def scenario():
        async def respond(request):
            payload = await request.json()
            assert payload['input']['messages'][-1]['text'] == '我醒啦'
            return web.json_response(body, status=status)
        app = web.Application()
        app.router.add_post('/v1/companion/shadow', respond)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setenv('OLIVIA_SEMANTIC_SHADOW_URL', f'http://127.0.0.1:{port}/v1/companion/shadow')
        try:
            result = await observe([], '我醒啦', ['text'])
            assert result['status'] == expected and result['fallback'] is True
            assert 'private' not in json.dumps(result)
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


def test_redirect_is_not_followed(monkeypatch):
    async def scenario():
        calls = []
        async def redirect(request):
            calls.append(request.path)
            raise web.HTTPFound('/should-not-be-called')
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', redirect)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setenv('OLIVIA_SEMANTIC_SHADOW_URL', f'http://127.0.0.1:{port}/v1/companion/shadow')
        try:
            result = await observe([], '你好', ['text'])
            assert result['fallback'] is True
            assert calls == ['/v1/companion/shadow']
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


def test_truncated_history_is_not_silently_accepted():
    meta = dict(source='s', event_id='s:user', actor='user', evidence_kind='statement_only', truncated=True)
    messages = [dict(role='user', content='[历史消息 ' + json.dumps(meta) + ']\n历史片段'),
                dict(role='user', content='现在呢')]
    with pytest.raises(ValueError, match='context_insufficient'):
        shadow_input(messages, '现在呢', ['text'])


def test_background_recording_keeps_only_result_and_uses_existing_persistence(monkeypatch):
    from runtime.personal_chat import backend
    async def scenario():
        saved = []
        async def persist(server):
            saved.append(server)
        monkeypatch.setattr(backend, 'persist_chat', persist)
        future = asyncio.get_running_loop().create_future()
        future.set_result({'status': 'model_busy', 'fallback': True})
        row = {'content': 'existing input'}
        await backend._record_semantic_shadow('server', row, future)
        assert saved == ['server'] and row['content'] == 'existing input'
        assert row['semantic_shadow'] == future.result()
    asyncio.run(scenario())


@pytest.mark.parametrize('started', [False, True])
def test_shutdown_cancels_only_own_observers_and_prevents_late_writes(monkeypatch, started):
    from types import SimpleNamespace
    from runtime.personal_chat import backend
    async def scenario():
        saved = []
        async def persist(server):
            saved.append(server)
        async def delayed():
            await asyncio.Event().wait()
            return {'status': 'observed'}
        monkeypatch.setattr(backend, 'persist_chat', persist)
        server, other = SimpleNamespace(), SimpleNamespace()
        row, other_row = {}, {}
        own, foreign = asyncio.create_task(delayed()), asyncio.create_task(delayed())
        backend._start_semantic_shadow_recorder(server, row, own)
        backend._start_semantic_shadow_recorder(other, other_row, foreign)
        if started:
            await asyncio.sleep(0)
        await backend._stop_semantic_shadow(server)
        assert own.cancelled() and not foreign.cancelled()
        assert not saved and not row and not server._semantic_shadow_tasks
        await backend._stop_semantic_shadow(other)
    asyncio.run(scenario())


def test_pipeline_observation_cannot_replace_reply(monkeypatch):
    from tests.persona.test_reply_semantic_wiring import Engine, envelope
    from reply_orchestrator import ReplyRequest
    from runtime.reply.reply_context import ReplyMode, ReplyContext, TrustedTime
    from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
    from runtime.reply.reply_reviewer import NullReviewer
    from runtime.personal_chat.presentation import CURRENT
    from runtime.reply import semantic_shadow
    seen = []
    async def inspect(messages, user_text, kinds):
        seen.append(user_text)
        await asyncio.Event().wait()
        return {'status': 'model_busy', 'fallback': True}
    monkeypatch.setattr(semantic_shadow, 'observe', inspect)
    monkeypatch.setenv('OLIVIA_SEMANTIC_SHADOW_URL', 'http://127.0.0.1:18768/v1/companion/shadow')
    original = json.dumps(envelope(), ensure_ascii=False)
    async def scenario():
        token = CURRENT.set(dict(structured=True, raw_user_text='我醒啦', proactive=False))
        pipeline = ReplyPipeline(Engine(original), reviewer=NullReviewer(),
                                 rewriter=UnavailableRewriter(), discover_runtime_ports=False)
        try:
            result = await asyncio.wait_for(pipeline.run(ReplyRequest(content='我醒啦'),
                ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True,
                    trusted_time=TrustedTime(datetime(2026, 9, 26, tzinfo=timezone.utc)))), .5)
            assert result.text == original
            assert result.semantic_shadow_task is not None
            await asyncio.sleep(0)
            assert seen == ['我醒啦']
            assert not result.semantic_shadow_task.done()
            result.semantic_shadow_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await result.semantic_shadow_task
        finally:
            CURRENT.reset(token)
    asyncio.run(scenario())

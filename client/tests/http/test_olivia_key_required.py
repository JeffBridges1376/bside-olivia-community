import asyncio

import local_server
from runtime.reply import jev_billing


def test_send_and_preview_ask_for_the_olivia_key_before_creating_a_letter(monkeypatch):
    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED', '1')
    monkeypatch.setattr(jev_billing, '_account_key', lambda: None)
    monkeypatch.setattr(local_server, '_missing_memory_component', lambda: None)
    monkeypatch.setattr(local_server, '_proactive_busy', False)
    before = list(local_server.store.letters)
    for path in ('/toy/letter/send', '/toy/letter/route-preview'):
        result = asyncio.run(local_server.route('POST', path, {'content': 'synthetic letter'}, {}, defer_reply=True))
        assert result['code'] == 503
        assert result['data']['error_code'] == 'OLIVIA_KEY_REQUIRED'
        assert result['data']['retryable'] is False
    assert local_server.store.letters == before


def test_queued_letter_fails_with_key_required_when_the_key_disappears(monkeypatch):
    letter = {'letter_id': 'key-removed', 'content': 'synthetic', 'letter_status': 'PENDING'}
    monkeypatch.setattr(local_server.store, 'letters', [letter])
    monkeypatch.setattr(local_server, '_persist_store_state', lambda: None)

    async def generate(*_args, **_kwargs):
        raise ValueError('JEV_BILLING_ACCOUNT_UNAVAILABLE')

    monkeypatch.setattr(local_server, 'generate_reply', generate)
    assert asyncio.run(local_server._run_reply_job('key-removed', 'synthetic', idempotency_key=None)) is False
    assert letter['letter_status'] == 'FAILED' and letter['error_code'] == 'OLIVIA_KEY_REQUIRED'

    other = {'letter_id': 'other-failure', 'content': 'synthetic', 'letter_status': 'PENDING'}
    monkeypatch.setattr(local_server.store, 'letters', [other])

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError('synthetic outage')

    monkeypatch.setattr(local_server, 'generate_reply', unavailable)
    asyncio.run(local_server._run_reply_job('other-failure', 'synthetic', idempotency_key=None))
    assert other['error_code'] == 'LLM_UNAVAILABLE'

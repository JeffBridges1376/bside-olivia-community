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


def test_send_and_preview_stop_when_the_reply_writer_has_no_key(monkeypatch):
    """The account key can exist while replies still have no key (1.x upgrade)."""
    from llm_gateway import UnconfiguredAdapter
    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED', '1')
    monkeypatch.setattr(jev_billing, '_account_key', lambda: 'olivia-synthetic-account-key')
    monkeypatch.setattr(local_server, '_missing_memory_component', lambda: None)
    monkeypatch.setattr(local_server, '_proactive_busy', False)
    config, _gateway = local_server.letters_adapter._runtime
    monkeypatch.setattr(local_server.letters_adapter, '_runtime', (config, UnconfiguredAdapter()))
    before = list(local_server.store.letters)
    for path in ('/toy/letter/send', '/toy/letter/route-preview'):
        result = asyncio.run(local_server.route('POST', path, {'content': 'synthetic letter'}, {}, defer_reply=True))
        assert result['code'] == 503
        assert result['data']['error_code'] == 'REPLY_SERVICE_NOT_CONNECTED'
        assert result['data']['retryable'] is False
    assert local_server.store.letters == before


def test_reply_writer_check_reads_the_key_of_a_configured_gateway(monkeypatch):
    from types import SimpleNamespace
    keyed = SimpleNamespace(config=SimpleNamespace(requires_api_key=True), _key=lambda: 'synthetic')
    keyless = SimpleNamespace(config=SimpleNamespace(requires_api_key=True), _key=lambda: None)
    optional = SimpleNamespace(config=SimpleNamespace(requires_api_key=False), _key=lambda: None)
    for gateway, missing in ((keyed, False), (keyless, True), (optional, False)):
        monkeypatch.setattr(local_server.letters_adapter, '_runtime', (None, gateway))
        assert local_server._reply_writer_unavailable() is missing

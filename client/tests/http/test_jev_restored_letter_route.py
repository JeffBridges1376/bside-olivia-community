import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


def test_restored_letter_without_preflight_uses_jev_route(monkeypatch):
    import local_server as server
    from letter_triage import TriageResult
    letter = {'letter_id': 'restore-jev', 'reply_routes': {'voice_reply': True}}
    monkeypatch.setattr(server.store, 'letters', [letter])
    monkeypatch.setattr(server, '_persist_store_state', lambda: None)
    monkeypatch.setattr(server, 'letters_adapter', SimpleNamespace(_now=lambda: datetime.now(timezone.utc)))
    monkeypatch.setattr(server, 'receive_eligibility_from_letter', lambda _: SimpleNamespace(enabled=True))
    monkeypatch.setattr('runtime.reply.companion_runtime.configured_port', lambda: object())
    calls = []
    async def classify(content, routes):
        calls.append((content, routes))
        return TriageResult('normal', 'voice_reply', 'jev', 'completed', True)
    async def forbidden(*args, **kwargs):
        pytest.fail('restored letter must not use text-model routing')
    monkeypatch.setattr(server, '_classify_managed_route', classify)
    monkeypatch.setattr(server, 'emotion_triage', SimpleNamespace(classify=forbidden))
    class Routed(Exception):
        pass
    def stop(mode):
        raise Routed()
    monkeypatch.setattr(server, '_exact_reply_mode', stop)
    with pytest.raises(Routed):
        asyncio.run(server.generate_reply('restore-jev', '我想听语音'))
    assert calls == [('我想听语音', {'voice_reply': True})]

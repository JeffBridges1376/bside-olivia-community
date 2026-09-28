import asyncio
from types import SimpleNamespace

import pytest

from runtime.personal_chat import backend
from runtime import image_understanding


def test_consumer_or_photo_failure_does_not_stop_later_rows_or_future_ticks(monkeypatch):
    async def scenario():
        stop = asyncio.Event()
        rows = [dict(id='bad', image_world_status='PENDING'), dict(id='good', image_world_status='PENDING')]
        scans, writes, states, logs = [], [], [], []
        async def recover():
            scans.append(1)
            if len(scans) == 1:
                raise RuntimeError('private exception details')
            stop.set()
        async def commit(server, row):
            writes.append(row['id'])
            if row['id'] == 'bad' and len(scans) == 1:
                raise RuntimeError('private image details')
            row['image_world_status'] = 'COMMITTED'
        server = SimpleNamespace(store=SimpleNamespace(personal_chats=rows),
                                 _safe_log=lambda *a, **kw: logs.append(kw))
        runtime = dict(stop=stop, service=SimpleNamespace(recover=recover), status={}, errors={})
        monkeypatch.setattr(image_understanding, 'commit_image_memory', commit)
        monkeypatch.setattr(backend, '_publish_status', lambda s, r: states.append(dict(r['status'])))
        await asyncio.wait_for(backend._recover_chat_loop(server, runtime, interval_seconds=.001), 1)
        assert len(scans) == 2 and writes == ['bad', 'good', 'bad']
        assert states == [{'recovery': 'FAILED'}, {'recovery': 'READY'}]
        assert runtime['errors'] == {} and all(row['image_world_status'] == 'COMMITTED' for row in rows)
        assert 'private' not in str(logs)
    asyncio.run(scenario())


def test_recovery_cancellation_is_not_retried(monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        async def recover():
            entered.set()
            await asyncio.Event().wait()
        runtime = dict(stop=asyncio.Event(), service=SimpleNamespace(recover=recover), status={}, errors={})
        server = SimpleNamespace(store=SimpleNamespace(personal_chats=[]))
        task = asyncio.create_task(backend._recover_chat_loop(server, runtime))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not runtime['errors']
    asyncio.run(scenario())


def test_status_file_failure_cannot_terminate_recovery(monkeypatch):
    async def scenario():
        stop = asyncio.Event()
        scans, writes, logs = [], [], []
        async def recover():
            scans.append(1)
            if len(scans) == 2:
                stop.set()
        def write(path, text):
            writes.append(text)
            if len(writes) == 1:
                raise OSError('private path must not escape')
        from pathlib import Path
        server = SimpleNamespace(store=SimpleNamespace(personal_chats=[]),
            _state_root=lambda: Path('.'), _atomic_write_store_file=write,
            _safe_log=lambda *a, **kw: logs.append((a, kw)))
        runtime = dict(stop=stop, service=SimpleNamespace(recover=recover), status={}, errors={})
        await asyncio.wait_for(backend._recover_chat_loop(server, runtime, interval_seconds=.001), 1)
        assert len(scans) == len(writes) == 2
        assert logs and 'private path' not in str(logs)
        assert runtime['status']['recovery'] == 'READY'
    asyncio.run(scenario())


def test_recovery_error_is_visible_without_exposing_exception_text():
    server = SimpleNamespace(store=SimpleNamespace(personal_chats=[]))
    runtime = dict(status={'recovery': 'FAILED'}, errors={'recovery': 'IMAGE_STATE_UNAVAILABLE'})
    assert backend.reply_errors(server, runtime) == {'recovery': 'IMAGE_STATE_UNAVAILABLE'}
    runtime['errors']['recovery'] = 'private filesystem detail'
    assert backend.reply_errors(server, runtime) == {'recovery': 'PERSONAL_CHAT_UNAVAILABLE'}

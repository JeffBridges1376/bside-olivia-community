import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime
from tests.private_world.decisions import life_decision


@pytest.mark.parametrize('fails', [False, True])
def test_world_refresh_never_appraises_in_the_background(tmp_path, monkeypatch, fails):
    monkeypatch.delenv('OLIVIA_JEV_URL', raising=False)
    now = datetime(2026, 9, 5, 10, tzinfo=timezone.utc)

    class Gateway:
        async def complete(self, messages, **kwargs):
            if fails:
                raise OSError('offline')
            return SimpleNamespace(text=json.dumps(life_decision(messages)))

    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    runtime = DailyLifeRuntime(store, Gateway, lambda: '口味偏清淡。')
    seen = []

    async def appraise(timestamp):
        seen.append(timestamp)

    runtime.emotion.refresh_world = appraise
    runtime._refresh_emotion = appraise
    asyncio.run(runtime.refresh(now))
    asyncio.run(runtime.refresh(now))
    assert seen == []
    assert (store.snapshot(now)['current'] is None) == fails


def test_cancelled_refresh_does_not_start_emotion_call(tmp_path):
    async def run():
        entered = asyncio.Event()

        class Gateway:
            async def complete(self, messages, **kwargs):
                entered.set()
                await asyncio.Future()

        runtime = DailyLifeRuntime(DailyLifeStore(tmp_path / 'life.sqlite3'), Gateway, lambda: '')

        async def unexpected_call(now):
            pytest.fail('Cancellation must not start a new model request')

        runtime._refresh_emotion = unexpected_call
        task = asyncio.create_task(runtime.refresh(datetime(2026, 9, 5, 10, tzinfo=timezone.utc)))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

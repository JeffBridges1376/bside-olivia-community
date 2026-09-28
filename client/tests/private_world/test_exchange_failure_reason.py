import ast
import asyncio
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('reason,expected', [
    ('JEV_EXCHANGE_SLOT_CONFLICT', 'JEV_EXCHANGE_SLOT_CONFLICT'),
    ('JEV_INPUT_TOO_LARGE', 'JEV_INPUT_TOO_LARGE'),
    ('JEV_HTTP_503', 'JEV_HTTP_503'),
    ('DAILY_LIFE_BOUNDARY_UNAVAILABLE', 'DAILY_LIFE_BOUNDARY_UNAVAILABLE'),
    ('JEV_UNKNOWN_SECRET', 'DAILY_LIFE_VALUEERROR'),
    ('private text key=secret', 'DAILY_LIFE_VALUEERROR'),
])
def test_background_exchange_preserves_only_known_machine_failure_codes(monkeypatch, reason, expected):
    from runtime.personal_chat import contact_invitation
    monkeypatch.setattr(contact_invitation, 'status', lambda *args: {})
    async def failed(*args, **kwargs):
        raise ValueError(reason)
    source = Path(__file__).resolve().parents[2] / 'local_server.py'
    function = next(node for node in ast.parse(source.read_text(encoding='utf8')).body
                    if isinstance(node, ast.FunctionDef) and node.name == '_schedule_daily_life_exchange')
    namespace = dict(asyncio=asyncio, datetime=datetime, sqlite3=sqlite3, _re=re,
        daily_life_runtime=SimpleNamespace(consume_exchange=failed), daily_life_tasks={},
        store=SimpleNamespace(letters=[]), private_world_port=SimpleNamespace(snapshot=lambda: None),
        _persist_store_state=lambda: None)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
    letter = dict(letter_id='synthetic', reply_revision=1, daily_life_status='PENDING',
                  private_world_occurred_at='2026-09-28T00:00:00+00:00')
    async def run():
        namespace['_schedule_daily_life_exchange'](letter)
        await asyncio.gather(*tuple(namespace['daily_life_tasks'].values()))
    asyncio.run(run())
    assert letter['daily_life_status'] == 'PENDING'
    assert letter['daily_life_failure_reason'] == expected


@pytest.mark.parametrize('reason,expected', [
    ('JEV_UNAVAILABLE', 'JEV_UNAVAILABLE'),
    ('JEV_HTTP_503', 'JEV_HTTP_503'),
    ('LIFE_EPISODE_RECOVERY_INVALID', 'LIFE_EPISODE_RECOVERY_INVALID'),
    ('private provider response', 'DAILY_LIFE_EVALUATION_FAILED'),
])
def test_world_refresh_retains_safe_failure_code(tmp_path, reason, expected):
    from runtime.private_world.daily_life import DailyLifeStore
    from runtime.private_world.daily_life_runtime import DailyLifeRuntime
    class Gateway:
        async def complete(self, *args, **kwargs):
            raise RuntimeError(reason)
    runtime = DailyLifeRuntime(DailyLifeStore(tmp_path / 'world.db'), lambda: Gateway(), lambda: '[]')
    now = datetime.fromisoformat('2026-09-28T03:00:00+00:00')
    asyncio.run(runtime.refresh(now))
    assert runtime.snapshot(now)['last_failure_code'] == expected

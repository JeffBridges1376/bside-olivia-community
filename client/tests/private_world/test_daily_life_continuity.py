import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime
from tests.private_world.decisions import life_decision


def test_weekly_continuity_survives_restart_and_reaches_refresh(tmp_path):
    now = datetime(2026, 9, 26, 6, tzinfo=timezone.utc)
    path = tmp_path / 'life.sqlite3'
    store = DailyLifeStore(path)
    for day in range(1, 10):
        for slot in range(4):
            store.publish_day(f'day-{day}-{slot}',
                {'location': '家里', 'activity': '练琴', 'note': f'第{day}天的第{slot}次练习'}, [],
                occurred_at=now - timedelta(days=day, minutes=slot))
    store.publish_day('future', {'location': '家里', 'activity': '未来活动', 'note': 'FUTURE'}, [],
                      occurred_at=now + timedelta(days=1))
    store.record_exchange('reply:private:1', 'PRIVATE_USER_TEXT', '现在喝水。', [],
                          occurred_at=now - timedelta(hours=1), current_quote='现在喝水。')
    calls = []

    class Gateway:
        async def complete(self, messages, **kwargs):
            calls.append(messages)
            return SimpleNamespace(text=json.dumps(life_decision(messages)))

    runtime = DailyLifeRuntime(DailyLifeStore(path), Gateway, lambda: '喜欢音乐。')
    asyncio.run(runtime.refresh(now))
    assert runtime.error_code is None
    data = json.loads(calls[0][1]['content'])
    history = data['recent_life']
    assert 7 <= len(history) <= 14
    assert any(item['source_id'].startswith('day-6-') for item in history)
    assert not any(item['source_id'].startswith('day-8-') for item in history)
    assert 'FUTURE' not in json.dumps(history)
    assert 'PRIVATE_USER_TEXT' not in json.dumps(data)
    assert all(item['evidence_kind'] == 'published_life' for item in history)
    assert all(set(item) == {'source_id', 'occurred_at', 'activity', 'note', 'evidence_kind'} for item in history)

import asyncio
import pytest
from datetime import datetime, timedelta, timezone

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime


def test_new_statement_reconsiders_fresh_world_once_without_certifying_reply(tmp_path, monkeypatch):
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: None)
    monkeypatch.setattr('runtime.private_world.daily_life_runtime.configured_duties', lambda: None)
    now = datetime(2026, 9, 27, 5, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    store.publish_day('day:old', {'location': '家里', 'activity': '休息', 'note': '在家里休息。'}, [],
                      occurred_at=now - timedelta(minutes=5), activity_kind='rest')
    store.record_exchange('reply:latest', '去吃饭吧', '现在往厨房走。', [],
                          occurred_at=now, current_quote='现在往厨房走。')
    with store._db() as db:
        db.execute('INSERT INTO life_exchange_world_gate VALUES (?,?,?)', ('reply:latest', 'reconsider', '现在往厨房走。'))
    assert store.snapshot(now)['current']['activity'] == '休息'
    assert not store.snapshot(now)['stale']
    calls = []
    runtime = DailyLifeRuntime(store, lambda: None, lambda: '')

    async def complete(prompt, data, request_id, **kwargs):
        calls.append(data)
        return {'activity': {'kind': 'meal', 'place_id': 'home', 'focus': ''},
                'meal': {'slot': 'lunch', 'food': '面条', 'status': 'eating'}, 'project': None}

    monkeypatch.setattr(runtime, '_complete', complete)
    asyncio.run(runtime.refresh(now))
    assert runtime.error_code is None
    assert calls[0]['exchange_actions'][0]['evidence_kind'] == 'character_statement'
    assert store.snapshot(now)['current']['activity'] == '正在吃午饭'
    asyncio.run(runtime.refresh(now + timedelta(seconds=1)))
    assert len(calls) == 1


def test_exchange_actions_excludes_future_old_and_non_action_chat(tmp_path):
    now = datetime(2026, 9, 27, 5, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    for name, stamp in [('old', now-timedelta(hours=7)), ('future', now+timedelta(minutes=1)), ('new', now)]:
        store.record_exchange('reply:'+name, 'PRIVATE', '现在往厨房走。', [],
                              occurred_at=stamp, current_quote='现在往厨房走。')
        with store._db() as db:
            db.execute('INSERT INTO life_exchange_world_gate VALUES (?,?,?)', ('reply:'+name, 'reconsider', '现在往厨房走。'))
    store.record_exchange('reply:chat', 'PRIVATE', '谢谢。', [], occurred_at=now)
    result = store.pending_exchange_actions(now)
    assert [a['source_id'] for a in result] == ['reply:new']
    assert 'PRIVATE' not in str(result)
    assert not store.pending_exchange_actions(now, after=now)


@pytest.mark.parametrize('decision', ['none', 'reconsider'])
def test_delivered_exchange_gate_is_separate_durable_and_recovers_existing_source(tmp_path, monkeypatch, decision):
    now = datetime.now(timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    # Simulate a delivery recorded before the new gate was installed, including
    # a case whose exchange extractor found no current action.
    store.record_exchange('reply:existing', '去吃饭吧', '我这就去。', [], occurred_at=now)
    calls, scheduled = [], []

    class Port:
        async def ask(self, state, questions, *, purpose):
            calls.append((state, questions, purpose))
            return {'world_update': decision}

    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: Port())
    runtime = DailyLifeRuntime(store, lambda: None, lambda: '')
    monkeypatch.setattr(runtime, 'schedule_refresh', scheduled.append)
    for _ in range(2):
        assert not asyncio.run(runtime.consume_exchange('reply:existing', '去吃饭吧', '我这就去。', occurred_at=now))
    assert len(calls) == 1
    assert calls[0][2] == 'exchange-world-update'
    assert set(calls[0][0]) == {'occurred_at', 'assessed_at', 'user_text', 'delivered_reply', 'current', 'meals'}
    assert bool(scheduled) == (decision == 'reconsider')
    actions = DailyLifeStore(store.path).pending_exchange_actions(now)
    assert bool(actions) == (decision == 'reconsider')
    if actions:
        assert actions[0]['reply_text'] == '我这就去。'
        assert actions[0]['current'] is None


def test_failed_gate_does_not_schedule_or_mark_approved_and_can_retry(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    store.record_exchange('reply:existing', '去吃饭吧', '我这就去。', [], occurred_at=now)
    class Port:
        async def ask(self, *args, **kwargs):
            raise RuntimeError('JEV_UNAVAILABLE')
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: Port())
    runtime = DailyLifeRuntime(store, lambda: None, lambda: '')
    scheduled = []
    monkeypatch.setattr(runtime, 'schedule_refresh', scheduled.append)
    with pytest.raises(RuntimeError, match='JEV_UNAVAILABLE'):
        asyncio.run(runtime.consume_exchange('reply:existing', '去吃饭吧', '我这就去。', occurred_at=now))
    assert not scheduled
    assert not store.pending_exchange_actions(now)
    with store._db() as db:
        assert db.execute('SELECT count(*) FROM life_exchange_world_gate').fetchone()[0] == 0


@pytest.mark.parametrize(('user_text', 'reply_text', 'decision'), [
    ('吃完了吗？', '吃完了，碗也已经洗了。', 'reconsider'),
    ('吃完了吗？', '我还在吃。', 'none'),
    ('你刚才说什么？', '她说吃完了，碗也洗了。', 'none'),
    ('你接下来干嘛？', '等吃完再洗碗。', 'none'),
])
def test_completion_gate_contract_and_old_negative_migration(tmp_path, monkeypatch, user_text, reply_text, decision):
    now = datetime.now(timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    store.record_exchange('reply:existing', user_text, reply_text, [], occurred_at=now)
    with store._db() as db:
        db.execute('INSERT INTO life_exchange_world_gate VALUES (?,?,?)', ('reply:existing', 'none', reply_text))
    calls, scheduled = [], []
    class Port:
        async def ask(self, state, questions, *, purpose):
            calls.append(state)
            contract = questions['world_update']['instructions']
            assert '完成、停止、取消、失败或结果变化' in contract
            assert '不能因为完成说法尚未得到世界核验' in contract
            assert '只由用户问' in contract and '引用他人的' in contract and '假设' in contract
            assert state['delivered_reply'] == reply_text
            return {'world_update': decision}
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: Port())
    runtime = DailyLifeRuntime(store, lambda: None, lambda: '')
    monkeypatch.setattr(runtime, 'schedule_refresh', scheduled.append)
    for _ in range(2):
        asyncio.run(runtime.consume_exchange('reply:existing', user_text, reply_text, occurred_at=now))
    assert len(calls) == 1
    assert bool(scheduled) == (decision == 'reconsider')
    with store._db() as db:
        assert db.execute('SELECT version FROM life_exchange_world_gate_versions').fetchone()[0] == 2
        assert db.execute('SELECT decision FROM life_exchange_world_gate').fetchone()[0] == decision
    # The gate never publishes a meal or completes a world action by itself.
    assert not store.snapshot(now)['world']['meals']

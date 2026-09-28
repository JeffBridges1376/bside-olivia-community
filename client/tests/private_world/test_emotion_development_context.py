import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from runtime.memory.received_user_originals import ReceivedOriginal
from runtime.private_world.character_emotion_runtime import CharacterEmotionRuntime
from runtime.private_world.daily_life import DailyLifeStore


def test_appraisal_reads_development_at_each_original_time_not_delivery_time(tmp_path, monkeypatch):
    now = datetime(2026, 9, 27, 4, tzinfo=timezone.utc)
    earlier = now - timedelta(days=20)
    life = DailyLifeStore(tmp_path / 'life.sqlite')
    calls, reads = [], []

    def development(when):
        reads.append(when)
        return {'as_of': when.isoformat(), 'version': 'old' if when == earlier else 'new',
                'items': [] if when == earlier else [{'key': 'photography', 'stage': 'familiar'}]}

    monkeypatch.setattr(life, 'development_view', development, raising=False)

    async def complete(messages, **_):
        packet = json.loads(messages[-1]['content'])
        calls.append(packet)
        return SimpleNamespace(text=json.dumps({'appraisals': [
            {'source_id': s['source_id'], 'quote': '', 'reported_affect': None, 'reaction': 'none',
             'goal_or_need': None, 'action_tendency': 'none', 'concern': None, 'revises': None}
            for s in packet['assessment']['sources']]}))

    emotion = CharacterEmotionRuntime(life, lambda: SimpleNamespace(complete_structured_scoped=complete), lambda: '角色基线')
    receipts = [ReceivedOriginal('received-user:qq:' + key, '你觉得摄影怎么样', when, (), 'qq', key)
                for key, when in [('old', earlier), ('new', now)]]
    asyncio.run(emotion.evaluate_received(receipts, now=now))
    assert len(calls) == 1
    contexts = calls[0]['assessment']['source_contexts']
    assert contexts[receipts[0].source_id]['character_development']['version'] == 'old'
    assert contexts[receipts[1].source_id]['character_development']['version'] == 'new'
    assert earlier in reads and now in reads


def test_development_read_failure_stays_unknown_and_does_not_stop_emotion(tmp_path, monkeypatch):
    life = DailyLifeStore(tmp_path / 'life.sqlite')

    def unavailable(_):
        raise OSError('synthetic unavailable')

    monkeypatch.setattr(life, 'development_view', unavailable, raising=False)
    emotion = CharacterEmotionRuntime(life, lambda: None, lambda: '角色基线')
    now = datetime(2026, 9, 27, 4, tzinfo=timezone.utc)
    result = emotion._context({'occurred_at': now.isoformat()},
                              {'as_of': now.isoformat(), 'concerns': [], 'prior_appraisals': []})
    assert result['character_development']['status'] == 'unavailable'
    assert result['character_development']['items'] == []
    assert result['rhythm']


def test_late_development_while_model_waits_reappraises_before_commit(tmp_path, monkeypatch):
    life = DailyLifeStore(tmp_path / 'life.sqlite')
    now = datetime(2026, 9, 27, 4, tzinfo=timezone.utc)
    version = 'old'
    seen = []

    def development(when):
        return {'as_of': when.isoformat(), 'version': version, 'items': []}

    monkeypatch.setattr(life, 'development_view', development, raising=False)

    async def complete(messages, **_):
        nonlocal version
        packet = json.loads(messages[-1]['content'])
        source = packet['assessment']['sources'][0]
        seen.append(packet['assessment']['source_contexts'][source['source_id']]['character_development']['version'])
        reaction = 'pleased' if version == 'old' else 'calm'
        version = 'new'  # A concurrent committed correction changes this same as-of view.
        return SimpleNamespace(text=json.dumps({'appraisals': [
            {'source_id': source['source_id'], 'quote': source['text'], 'reported_affect': None,
             'reaction': reaction, 'goal_or_need': None, 'action_tendency': 'none', 'concern': None, 'revises': None}]}))

    emotion = CharacterEmotionRuntime(life, lambda: SimpleNamespace(complete_structured_scoped=complete), lambda: '角色基线')
    receipt = ReceivedOriginal('received-user:qq:race', '再聊聊摄影', now, (), 'qq', 'race')
    view = asyncio.run(emotion.evaluate_received([receipt], now=now))
    assert seen == ['old', 'new']
    assert [item['reaction'] for item in view['reactions']] == ['calm']

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import sqlite3

from runtime.private_world.current_affect import CurrentAffect, TTL, context


NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


class Store:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def _db(self):
        with sqlite3.connect(self.path) as db:
            yield db


class Port:
    def __init__(self, label='pleased'):
        self.label, self.calls = label, []

    async def ask(self, state, questions, *, purpose):
        self.calls.append(state)
        if self.label == 'error':
            raise RuntimeError('provider unavailable')
        return {'label': self.label, 'reason': 'unknown' if self.label == 'unknown' else 'life'}


def packet():
    return context({'current': {'note': '练习完成'}, 'stale': False,
                    'rhythm': {'phase': 'day', 'local_time': NOW.isoformat()},
                    'moments': [{'note': '练习完成'}]}, {'reactions': []}, '角色')


def test_state_persists_without_user_reaction_and_read_never_infers(tmp_path):
    affect, port = CurrentAffect(Store(tmp_path/'state.db')), Port()
    asyncio.run(affect.refresh(port, now=NOW, packet_factory=packet))
    for _ in range(4):
        assert affect.view(NOW)['label'] == 'pleased'
    assert len(port.calls) == 1
    again = CurrentAffect(Store(tmp_path/'state.db'))
    asyncio.run(again.refresh(port, now=NOW+timedelta(minutes=2), packet_factory=packet))
    assert len(port.calls) == 1
    assert again.view(NOW)['label'] == 'pleased'


def test_life_changes_and_expiry_refresh_but_clock_only_does_not(tmp_path):
    affect, port = CurrentAffect(Store(tmp_path/'state.db')), Port()
    value = packet()
    asyncio.run(affect.refresh(port, now=NOW, packet_factory=lambda: value))
    value['published_moments'].append({'note': '练习遇到困难'})
    port.label = 'frustrated'
    asyncio.run(affect.refresh(port, now=NOW+timedelta(minutes=1), packet_factory=lambda: value))
    assert affect.view(NOW+timedelta(minutes=1))['label'] == 'frustrated'
    assert len(port.calls) == 2
    asyncio.run(affect.refresh(port, now=NOW+TTL+timedelta(minutes=2), packet_factory=lambda: value))
    assert len(port.calls) == 3
    assert context({'rhythm': {'phase': 'day', 'local_time': 'a'}}, {}, '') == context(
        {'rhythm': {'phase': 'day', 'local_time': 'b'}}, {}, '')


def test_unknown_is_not_calm_failure_keeps_last_state_and_backs_off(tmp_path):
    affect, port = CurrentAffect(Store(tmp_path/'state.db')), Port('unknown')
    asyncio.run(affect.refresh(port, now=NOW, packet_factory=packet))
    assert affect.view(NOW)['label'] is None
    port.label = 'pleased'
    asyncio.run(affect.refresh(port, now=NOW+TTL, packet_factory=packet))
    port.label = 'error'
    asyncio.run(affect.refresh(port, now=NOW+TTL*2, packet_factory=packet))
    assert affect.view(NOW+TTL*2)['status'] == 'stale'
    assert affect.view(NOW+TTL*2)['label'] == 'pleased'
    asyncio.run(affect.refresh(port, now=NOW+TTL*2+timedelta(seconds=1), packet_factory=packet))
    assert len(port.calls) == 3
    assert affect.view(NOW)['label'] is None


def test_prepared_unknown_keeps_historical_mood_stale_without_another_ask(tmp_path):
    affect, port = CurrentAffect(Store(tmp_path/'unknown.db')), Port('pleased')
    asyncio.run(affect.refresh(port, now=NOW, packet_factory=packet))
    later = NOW+timedelta(minutes=1)
    plan = affect.prepare(packet(), now=later)
    asyncio.run(affect.refresh(port, now=later, packet_factory=packet, prepared=plan,
                              choices={'label': 'unknown', 'reason': 'unknown'}))
    view = affect.view(later)
    assert view['label'] == 'pleased' and view['status'] == 'stale'
    assert view['as_of'] == NOW.isoformat()
    reopened = CurrentAffect(affect.store)
    asyncio.run(reopened.refresh(port, now=later+timedelta(minutes=1), packet_factory=packet))
    assert len(port.calls) == 1
    assert reopened.view(later)['status'] == 'stale'


def test_legacy_unknown_check_cannot_make_old_label_available(tmp_path):
    import json
    affect = CurrentAffect(Store(tmp_path/'legacy-unknown.db'))
    payload = dict(label='frustrated', as_of=NOW.isoformat(), reason='old', basis={'context_digest':'old'})
    later = NOW+timedelta(minutes=1)
    with affect.store._db() as db:
        db.execute('INSERT INTO character_current_affect VALUES (1,?,?,?,?,?)',
                   (json.dumps(payload), 'new', later.timestamp(), (later+TTL).timestamp(), 0))
    assert affect.view(later)['status'] == 'stale'
    assert json.loads(affect._row()[0]) == payload


def test_concurrent_refresh_coalesces(tmp_path):
    affect, port = CurrentAffect(Store(tmp_path/'state.db')), Port()
    async def run():
        await asyncio.gather(*(affect.refresh(port, now=NOW, packet_factory=packet) for _ in range(5)))
    asyncio.run(run())
    assert len(port.calls) == 1


def test_concrete_episode_can_supply_mood_basis_without_user_message(tmp_path):
    affect = CurrentAffect(Store(tmp_path/'state.db'))
    value = packet()
    value['world']['recent_episodes'] = [{'source_id': 'life:practice:1',
        'result': {'status': 'unfinished', 'detail': '放慢速度再练，衔接仍然不稳。'}}]

    class EpisodePort:
        async def ask(self, state, questions, *, purpose):
            assert state['reactions'] == []
            assert state['world']['recent_episodes'][0]['result']['status'] == 'unfinished'
            assert questions['reason']['criteria']['episode_0_result'] == {'state_path': ['world', 'recent_episodes', 0, 'result', 'detail']}
            return {'label': 'frustrated', 'reason': 'episode_0_result'}

    asyncio.run(affect.refresh(EpisodePort(), now=NOW, packet_factory=lambda: value))
    observed = affect.view(NOW)
    assert observed['label'] == 'frustrated'
    assert observed['reason'] == '放慢速度再练，衔接仍然不稳。'
    assert 'life:practice:1' in observed['basis']['source_ids']


def test_world_lifecycle_updates_without_incoming_message_and_snapshot_only_reads(tmp_path, monkeypatch):
    from runtime.reply import jev_questions
    from runtime.private_world.daily_life import DailyLifeStore
    from runtime.private_world.character_emotion_runtime import CharacterEmotionRuntime
    from runtime.private_world.daily_life_runtime import DailyLifeRuntime
    port = Port('calm')
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    life = DailyLifeStore(tmp_path/'life.db')
    emotion = CharacterEmotionRuntime(life, lambda: None, lambda: '音乐专业大学生')
    asyncio.run(emotion.refresh_world(NOW))
    assert len(port.calls) == 1
    assert 'world' in port.calls[0] and 'rhythm' in port.calls[0]
    runtime = DailyLifeRuntime(life, lambda: None, lambda: '音乐专业大学生')
    runtime._emotion = emotion
    assert runtime.snapshot(NOW)['emotion']['current_affect']['label'] == 'calm'
    asyncio.run(emotion.refresh_world(NOW+timedelta(seconds=30)))
    assert len(port.calls) == 1


def test_inflight_changed_canonical_state_is_not_committed(tmp_path):
    affect = CurrentAffect(Store(tmp_path/'state.db'))
    value = packet()
    class ChangingPort(Port):
        async def ask(self, *args, **kwargs):
            value['current'] = {'note': '刚收到练习失败的结果'}
            return {'label': 'pleased', 'reason': 'life'}
    asyncio.run(affect.refresh(ChangingPort(), now=NOW, packet_factory=lambda: value))
    assert affect.view(NOW)['label'] is None


def test_episode_process_is_available_as_specific_mood_basis(tmp_path):
    affect = CurrentAffect(Store(tmp_path/'state.db'))
    value = packet()
    value['world']['recent_episodes'] = [{'source_id': 'practice:1',
        'process': [{'obstacle': '指法不顺', 'response': '分段慢练', 'outcome': '终于弹顺'}],
        'result': {'status': 'completed', 'detail': '完成困难段落'}}]
    class EpisodePort:
        async def ask(self, state, questions, *, purpose):
            assert '优先依据遇到的障碍' in state['contract']
            assert questions['reason']['criteria']['episode_0_process_0'] == {'state_path': ['world', 'recent_episodes', 0, 'process', 0]}
            return {'label': 'pleased', 'reason': 'episode_0_process_0'}
    asyncio.run(affect.refresh(EpisodePort(), now=NOW, packet_factory=lambda: value))
    assert affect.view(NOW)['reason'] == '指法不顺；分段慢练；终于弹顺'
    assert affect.view(NOW)['basis']['source_ids'] == ['practice:1']


def test_legacy_internal_reason_is_cleaned_only_for_display(tmp_path):
    import json
    store = Store(tmp_path/'state.db')
    affect = CurrentAffect(store)
    original = {'label': 'calm', 'as_of': NOW.isoformat(),
                'reason': '当前身体感受与作息的影响（不声称已睡着）', 'basis': {}}
    with store._db() as db:
        db.execute('INSERT INTO character_current_affect VALUES(1,?,?,?,?,?)',
                   (json.dumps(original), 'fixture', NOW.timestamp(), NOW.timestamp()+3600, 0))
    assert affect.view(NOW)['reason'] == '当前身体感受与作息的影响'
    assert json.loads(affect._row()[0]) == original


def test_reason_references_preserve_complete_episode_and_reduce_input(tmp_path):
    import copy
    import json
    affect = CurrentAffect(Store(tmp_path/'state.db'))
    value = packet()
    episode = {'source_id': 'practice:measured', 'occurred_at': NOW.isoformat(),
               'actor': 'character', 'evidence_kind': 'observed_episode',
               'process': [{'obstacle': 'Uneven rhythm. '*24,
                            'response': 'Practised slowly. '*24,
                            'outcome': 'Improved timing. '*24}],
               'result': {'status': 'completed', 'detail': 'Finished the difficult passage. '*24}}
    value['world']['recent_episodes'] = [episode]
    captured = []

    class EvidencePort:
        async def ask(self, state, questions, *, purpose):
            assert state['world']['recent_episodes'] == [episode]
            assert state['current_is_stale'] is False
            assert 'previous_affect' in state
            captured.append(dict(state=state, questions=questions, purpose=purpose))
            return {'label': 'pleased', 'reason': 'episode_0_result'}

    asyncio.run(affect.refresh(EvidencePort(), now=NOW, packet_factory=lambda: value))
    assert affect.view(NOW)['reason'] == episode['result']['detail'][:240]
    referenced = captured[0]
    inline = copy.deepcopy(referenced)
    for key, choice in inline['questions']['reason']['criteria'].items():
        if not isinstance(choice, dict):
            continue
        original = inline['state']
        for part in choice['state_path']:
            original = original[part]
        if isinstance(original, dict):
            original = '；'.join(str(original[k]) for k in ('obstacle', 'response', 'outcome') if original.get(k))
        inline['questions']['reason']['criteria'][key] = original
    size = lambda item: len(json.dumps(item, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    print('current_affect_repeated_criteria_bytes', size(inline), 'reference_bytes', size(referenced))
    assert size(referenced) < size(inline) - 1000

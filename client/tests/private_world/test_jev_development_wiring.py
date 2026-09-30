"""Development candidates still cross the real local evidence/transaction boundary."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from runtime.private_world import daily_life_runtime as runtime_module
from runtime.private_world.character_development import digest
from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime


NOW = datetime(2026, 9, 5, 4, tzinfo=timezone.utc)
TOPIC = {'key': 'photography', 'label': '摄影', 'kind': 'interest', 'baseline': 'neutral', 'anchor': False}
PERSONA = json.dumps([{'development': [TOPIC]}], ensure_ascii=False)
USER = '我们今天一起拍完照片，讨论了窗边构图。'
REPLY = '这次摄影构图让我觉得很有意思。'


def candidate(**changes):
    return {**{'key': 'photography', 'stance': 'positive', 'user_quote': USER,
               'character_quote': REPLY, 'experience_quote': USER,
               'episode_id': 'shared:photo', 'withdraws': None}, **changes}


def extraction(user=USER, reply=REPLY):
    return {'updates': [{'id': 'photo', 'title': '一起拍照', 'detail': user, 'status': 'completed',
                         'kind': 'shared', 'actor': 'user', 'quote': user}],
            'relationship': {'kind': 'shared_experience', 'user_quote': user, 'reply_quote': reply}}


class Duty:
    def __init__(self, candidates=None, *, error=None, bad_digest=False):
        self.candidates = [candidate()] if candidates is None else candidates
        self.error, self.bad_digest, self.calls = error, bad_digest, []

    async def evaluate(self, kind, packet):
        self.calls.append((kind, deepcopy(packet)))
        values = self.candidates(packet) if callable(self.candidates) else self.candidates
        return SimpleNamespace(decision=None if self.error else {'candidates': deepcopy(values)},
                               error_code=self.error, input_digest='0' * 64 if self.bad_digest else digest(packet))


def setup(tmp_path, monkeypatch, duty, *, output=None):
    monkeypatch.setattr(runtime_module, 'configured_duties', lambda: duty, raising=False)
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    runtime = DailyLifeRuntime(store, lambda: None, lambda: PERSONA)
    calls = []
    async def complete(prompt, data, request_id, **kwargs):
        calls.append((prompt, deepcopy(data), kwargs))
        return deepcopy(extraction() if output is None else output)
    async def no_emotion(now):
        pass
    runtime._complete = complete
    return store, runtime, calls


def consume(runtime, source='reply:jev:1', **kwargs):
    return asyncio.run(runtime.consume_exchange(source, USER, REPLY, occurred_at=NOW, **kwargs))


def event_count(store):
    with store._db() as db:
        return db.execute('SELECT COUNT(*) FROM character_development_events').fetchone()[0]


def seed(store, source='reply:seed', when=NOW - timedelta(days=1)):
    store.configure_development(PERSONA)
    data = extraction()
    store.record_exchange(source, USER, REPLY, data['updates'], occurred_at=when,
                          relationship=data['relationship'], development=[candidate()])


def test_exchange_jev_owns_candidates_with_original_hash_and_validated_episode(tmp_path, monkeypatch):
    duty = Duty()
    store, runtime, old_calls = setup(tmp_path, monkeypatch, duty)
    assert consume(runtime)
    kind, packet = duty.calls[0]
    assert kind == 'exchange'
    assert packet == {'mode': 'exchange', 'topics': [TOPIC], 'source_id': 'reply:jev:1',
                      'source_hash': digest([USER, REPLY]), 'as_of': NOW.isoformat(),
                      'user_text': USER, 'character_text': REPLY, 'origin': 'user',
                      'relationship_kind': 'shared_experience',
                      'episodes': [{'episode_id': 'shared:photo', 'description': USER}],
                      'withdrawal_candidates': []}
    assert 'development_topics' not in old_calls[0][1]
    assert 'development_episodes' not in old_calls[0][1]
    assert 'development 默认为' not in old_calls[0][0]
    assert 'boundaries、development 是数组' not in old_calls[0][0]
    assert store.development_view(NOW)['items'][0]['stage'] == 'trying'
    assert not consume(runtime)
    assert len(duty.calls) == event_count(store) == 1


@pytest.mark.parametrize('error,bad_digest', [('JEV_UNAVAILABLE', False), ('JEV_HTTP_404', False),
                                            ('JEV_HTTP_503', False), (None, True)])
def test_failure_does_not_mark_processed_or_fallback_and_restart_retries(tmp_path, monkeypatch, error, bad_digest):
    duty = Duty(error=error, bad_digest=bad_digest)
    store, runtime, old_calls = setup(tmp_path, monkeypatch, duty)
    with pytest.raises(RuntimeError, match='JEV_'):
        consume(runtime)
    assert not store.has_source('reply:jev:1')
    assert event_count(store) == 0
    assert len(old_calls) == len(duty.calls) == 1
    duty.error, duty.bad_digest = None, False
    restarted = DailyLifeRuntime(DailyLifeStore(store.path), lambda: None, lambda: PERSONA)
    restarted._complete = runtime._complete
    assert consume(restarted)
    assert event_count(store) == 1


def test_configuration_failure_cannot_reactivate_old_extractor(tmp_path, monkeypatch):
    store, runtime, calls = setup(tmp_path, monkeypatch, Duty())
    def broken():
        raise ValueError('JEV_CONFIGURATION_INVALID')
    monkeypatch.setattr(runtime_module, 'configured_duties', broken)
    with pytest.raises((ValueError, RuntimeError), match='JEV_CONFIGURATION_INVALID'):
        consume(runtime)
    assert calls == [] and not store.has_source('reply:jev:1')


def test_world_configuration_failure_never_calls_legacy_writer(tmp_path, monkeypatch):
    store, runtime, calls = setup(tmp_path, monkeypatch, Duty())
    def broken():
        raise ValueError('JEV_CONFIGURATION_INVALID')
    monkeypatch.setattr(runtime_module, 'configured_duties', broken)
    asyncio.run(runtime.refresh(NOW))
    assert runtime.error_code is not None and calls == []
    assert store.snapshot(NOW)['current'] is None


@pytest.mark.parametrize('changes', [
    {'key': 'identity'}, {'experience_quote': '不存在的引文'}, {'episode_id': 'shared:invented'},
    {'withdraws': 'reply:other-person'},
])
def test_jev_candidate_cannot_bypass_ledger_and_invalid_batch_is_atomic(tmp_path, monkeypatch, changes):
    duty = Duty([candidate(**changes)])
    store, runtime, calls = setup(tmp_path, monkeypatch, duty)
    with pytest.raises(RuntimeError, match='JEV_'):
        consume(runtime)
    assert not store.has_source('reply:jev:1') and event_count(store) == 0
    assert store.exchange_state(now=NOW)['shared'] == []
    assert len(calls) == len(duty.calls) == 1


def test_empty_candidates_are_valid_without_manufactured_growth(tmp_path, monkeypatch):
    duty = Duty([])
    store, runtime, _ = setup(tmp_path, monkeypatch, duty, output={'updates': [], 'relationship': None})
    assert consume(runtime)
    assert event_count(store) == 0


def test_enabled_old_model_cannot_also_emit_development(tmp_path, monkeypatch):
    duty = Duty()
    store, runtime, _ = setup(tmp_path, monkeypatch, duty, output={**extraction(), 'development': [candidate()]})
    with pytest.raises(ValueError, match='RESPONSE_INVALID'):
        consume(runtime)
    assert not duty.calls and not store.has_source('reply:jev:1')


def test_withdrawal_catalog_excludes_future_damaged_and_already_withdrawn(tmp_path, monkeypatch):
    duty = Duty([])
    store, runtime, _ = setup(tmp_path, monkeypatch, duty)
    seed(store)
    seed(store, 'reply:future', NOW + timedelta(days=1))
    seed(store, 'reply:damaged', NOW - timedelta(hours=1))
    with store._db() as db:
        db.execute("UPDATE life_moments SET payload=json_set(payload,'$.digest','bad') WHERE source_id='reply:damaged'")
    assert consume(runtime)
    rows = duty.calls[0][1]['withdrawal_candidates']
    assert [r['source_id'] for r in rows] == ['reply:seed']
    assert rows[0]['experience_quote'] == USER and rows[0]['character_quote'] == REPLY
    user, reply = '更正一下，那次共同拍照并未发生。', '明白，撤回那次体验评价。'
    duty.candidates = [candidate(user_quote=user, character_quote=reply, experience_quote=user,
                                 episode_id=None, withdraws='reply:seed')]
    async def extract(*args, **kwargs):
        return {'updates': [], 'relationship': None}
    runtime._complete = extract
    assert asyncio.run(runtime.consume_exchange('reply:withdraw', user, reply, occurred_at=NOW + timedelta(minutes=1)))
    assert store.development_view(NOW + timedelta(minutes=1))['items'] == []
    duty.candidates = []
    assert asyncio.run(runtime.consume_exchange('reply:after', USER, REPLY, occurred_at=NOW + timedelta(minutes=2)))
    assert duty.calls[-1][1]['withdrawal_candidates'] == []


def test_delayed_withdrawal_cannot_use_target_after_receipt(tmp_path, monkeypatch):
    duty = Duty([candidate(episode_id=None, withdraws='reply:seed')])
    store, runtime, _ = setup(tmp_path, monkeypatch, duty, output={'updates': [], 'relationship': None})
    seed(store, when=NOW - timedelta(hours=1))
    with pytest.raises(RuntimeError, match='JEV_'):
        consume(runtime, received_at=NOW - timedelta(days=2))
    assert duty.calls[0][1]['withdrawal_candidates'] == []
    assert not store.has_source('reply:jev:1')


def world_setup(tmp_path, monkeypatch, duty):
    from runtime.private_world.character_emotion import CharacterEmotionStore
    store, runtime, calls = setup(tmp_path, monkeypatch, duty,
        output={'activity': {'kind': 'rest', 'place_id': 'home', 'focus': ''}, 'meal': None, 'project': None})
    store.configure_development(PERSONA)
    note = '在窗边拍照，尝试不同的构图。'
    store.publish_day('day:photo', {'location': '家里', 'activity': '拍照', 'note': note}, [],
                      occurred_at=NOW - timedelta(hours=5), activity_kind='creative')
    emotion = CharacterEmotionStore(store.path)
    emotion.publish('day:photo')
    emotion.commit(emotion.assessment(['day:photo'], now=NOW), {'appraisals': [{
        'source_id': 'day:photo', 'quote': note, 'reaction': 'pleased', 'action_tendency': 'continue',
        'goal_or_need': None, 'concern': None, 'revises': None, 'reported_affect': None}]})
    duty.candidates = [{'source_id': 'day:photo', 'key': 'photography', 'stance': 'positive',
                        'quote': note, 'reason': '具体构图体验有趣。'}]
    return store, runtime, calls


def test_world_jev_receives_exact_frozen_basis_and_old_schema_has_no_development(tmp_path, monkeypatch):
    duty = Duty()
    store, runtime, calls = world_setup(tmp_path, monkeypatch, duty)
    expected = store.development_world_assessment(NOW)
    asyncio.run(runtime.refresh(NOW))
    assert runtime.error_code is None
    assert duty.calls == [('world', {'mode': 'world', 'topics': [TOPIC], 'basis': expected})]
    source = expected['sources'][0]
    assert source['source_hash'] == digest(source['world'])
    assert 'development_basis' not in calls[0][1]
    assert 'development' not in calls[0][2]['response_format']['json_schema']['schema']['properties']
    assert event_count(store) == 1
    asyncio.run(runtime.refresh(NOW))
    assert len(duty.calls) == event_count(store) == 1


@pytest.mark.parametrize('error', ['JEV_UNAVAILABLE', 'JEV_HTTP_404', 'JEV_HTTP_503'])
def test_world_transient_failure_rolls_back_and_retry_is_idempotent(tmp_path, monkeypatch, error):
    duty = Duty(error=error)
    store, runtime, calls = world_setup(tmp_path, monkeypatch, duty)
    asyncio.run(runtime.refresh(NOW))
    assert runtime.error_code is not None
    assert store.snapshot(NOW)['current']['source_id'] == 'day:photo'
    assert event_count(store) == 0 and len(calls) == 1
    duty.error = None
    asyncio.run(runtime.refresh(NOW + timedelta(minutes=3)))
    assert runtime.error_code is None and event_count(store) == 1
    assert len(calls) == len(duty.calls) == 2


def test_disabled_keeps_legacy_development_extraction(tmp_path, monkeypatch):
    store, runtime, calls = setup(tmp_path, monkeypatch, None,
                                 output={**extraction(), 'development': [candidate()]})
    assert consume(runtime)
    assert calls[0][1]['development_topics'] == [TOPIC]
    assert event_count(store) == 1


def actual_port(monkeypatch, values):
    from runtime.reply.companion_duties import JevDutiesPort
    port = JevDutiesPort('http://127.0.0.1:19491/v1/companion/decide')
    calls = []
    def request(kind, input_json):
        packet = json.loads(input_json)
        calls.append((kind, packet))
        candidates = values(packet) if callable(values) else values
        return {'schema_version': 'companion-experience-appraisal/1', 'decision': {'candidates': candidates},
                'input_digest': digest(packet), 'backend': 'jev', 'model': 'jev-1.13.0',
                'status': 'valid_contract', 'contract_valid': True, 'action_executed': False,
                'production_approved': False, 'latency_ms': 1, 'api_calls': 2, 'usage': {'input_tokens': 10}}
    monkeypatch.setattr(port, '_request', request)  # no sockets; all real request/response checks remain
    return port, calls


@pytest.mark.parametrize('kind', ['exchange', 'world'])
def test_real_port_validator_to_ledger_preserves_complete_source(tmp_path, monkeypatch, kind):
    port, requests = actual_port(monkeypatch, [candidate()])
    if kind == 'exchange':
        store, runtime, _ = setup(tmp_path, monkeypatch, port)
        assert consume(runtime)
        assert requests[0][1]['source_hash'] == digest([USER, REPLY])
    else:
        unused = Duty()
        store, runtime, _ = world_setup(tmp_path, monkeypatch, unused)
        monkeypatch.setattr(runtime_module, 'configured_duties', lambda: port)
        port, requests = actual_port(monkeypatch, unused.candidates)
        basis = store.development_world_assessment(NOW)
        asyncio.run(runtime.refresh(NOW))
        assert runtime.error_code is None
        assert requests[0][1]['basis'] == basis
    assert event_count(store) == 1 and len(requests) == 1


def test_real_validator_no_truncation_of_long_original_and_no_processed_marker(tmp_path, monkeypatch):
    port, requests = actual_port(monkeypatch, [])
    store, runtime, calls = setup(tmp_path, monkeypatch, port, output={'updates': [], 'relationship': None})
    user = USER + ('字' * 8000)
    with pytest.raises(RuntimeError, match='JEV_INPUT_INVALID'):
        asyncio.run(runtime.consume_exchange('reply:long', user, REPLY, occurred_at=NOW))
    assert requests == [] and len(calls) == 1
    assert not store.has_source('reply:long')


def test_real_validator_rejects_unknown_withdrawal_even_with_valid_current_quotes(tmp_path, monkeypatch):
    port, requests = actual_port(monkeypatch, [candidate(episode_id=None, withdraws='reply:elsewhere')])
    store, runtime, _ = setup(tmp_path, monkeypatch, port, output={'updates': [], 'relationship': None})
    other = DailyLifeStore(tmp_path / 'other-character.sqlite3')
    seed(other, 'reply:elsewhere')
    with pytest.raises(RuntimeError, match='JEV_RESPONSE_INVALID'):
        consume(runtime)
    assert requests[0][1]['withdrawal_candidates'] == []
    assert not store.has_source('reply:jev:1') and event_count(store) == 0
    assert event_count(other) == 1


def test_real_validator_withdrawal_keeps_old_negative_stance_without_new_vote(tmp_path, monkeypatch):
    user, reply = '更正，那次共同摄影并未发生。', '我明白，之前的不适评价应该撤回。'
    port, requests = actual_port(monkeypatch, [candidate(stance='negative', user_quote=user,
        character_quote=reply, experience_quote=user, episode_id=None, withdraws='reply:negative')])
    store, runtime, _ = setup(tmp_path, monkeypatch, port, output={'updates': [], 'relationship': None})
    store.configure_development(PERSONA)
    data = extraction()
    store.record_exchange('reply:negative', USER, REPLY, data['updates'], occurred_at=NOW - timedelta(days=1),
                          relationship=data['relationship'], development=[candidate(stance='negative')])
    assert asyncio.run(runtime.consume_exchange('reply:withdraw', user, reply, occurred_at=NOW))
    assert requests[0][1]['withdrawal_candidates'][0]['stance'] == 'negative'
    assert event_count(store) == 2 and store.development_view(NOW)['items'] == []
    assert not asyncio.run(runtime.consume_exchange('reply:withdraw', user, reply, occurred_at=NOW))
    assert len(requests) == 1 and event_count(store) == 2


def test_world_wire_epoch_conversion_does_not_mutate_frozen_local_basis(tmp_path, monkeypatch):
    store, _, _ = world_setup(tmp_path, monkeypatch, Duty())
    basis = store.development_world_assessment(NOW)
    basis['as_of'] = NOW.timestamp()
    basis['sources'][0]['occurred_at'] = (NOW - timedelta(hours=5)).timestamp()
    original = deepcopy(basis)
    packet = runtime_module._world_development_packet([TOPIC], basis)
    assert basis == original
    assert packet['basis']['as_of'] == NOW.isoformat()
    assert packet['basis']['sources'][0]['occurred_at'] == (NOW - timedelta(hours=5)).isoformat()
    assert packet['basis']['sources'][0]['world'] == original['sources'][0]['world']
    assert digest(packet['basis']['sources'][0]['world']) == original['sources'][0]['source_hash']


def test_world_stale_appraisal_is_rejected_without_partial_world_publish(tmp_path, monkeypatch):
    from runtime.private_world.character_emotion import CharacterEmotionStore
    duty = Duty()
    store, runtime, calls = world_setup(tmp_path, monkeypatch, duty)
    original = duty.evaluate
    async def changing(kind, packet):
        result = await original(kind, packet)
        emotion = CharacterEmotionStore(store.path)
        source, quote = 'reply:correction:user', '此前拍照的体验理解不准确。'
        emotion.receive(source, quote, occurred_at=NOW)
        emotion.commit(emotion.assessment([source], now=NOW), {'appraisals': [{
            'source_id': source, 'quote': quote, 'reaction': 'none', 'action_tendency': 'none',
            'goal_or_need': None, 'concern': None, 'reported_affect': None,
            'revises': {'source_id': 'day:photo', 'action': 'withdraw'}}]})
        return result
    duty.evaluate = changing
    asyncio.run(runtime.refresh(NOW))
    assert runtime.error_code is not None
    assert store.snapshot(NOW)['current']['source_id'] == 'day:photo'
    assert event_count(store) == 0 and len(calls) == len(duty.calls) == 1


def test_no_partial_commit_when_valid_candidate_precedes_invalid_candidate(tmp_path, monkeypatch):
    duty = Duty([candidate(), candidate(key='unknown')])
    store, runtime, _ = setup(tmp_path, monkeypatch, duty)
    with pytest.raises(RuntimeError, match='JEV_RESPONSE_INVALID'):
        consume(runtime)
    assert event_count(store) == 0 and not store.has_source('reply:jev:1')


def test_unverified_new_episode_is_rejected_before_jev_sees_it(tmp_path, monkeypatch):
    output = extraction()
    output['updates'][0]['quote'] = '当前正文中没有发生的活动。'
    duty = Duty()
    store, runtime, calls = setup(tmp_path, monkeypatch, duty, output=output)
    with pytest.raises(ValueError, match='EVIDENCE_INVALID'):
        consume(runtime)
    assert not duty.calls and not store.has_source('reply:jev:1')
    assert len(calls) == 2  # only the existing life extractor's correction allowance


def test_jev_invalid_exchange_does_not_replay_completed_pipeline(tmp_path, monkeypatch):
    from runtime.reply import jev_questions
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: object())
    output = extraction()
    output['updates'][0]['quote'] = '当前正文中没有发生的活动。'
    duty = Duty()
    store, runtime, calls = setup(tmp_path, monkeypatch, duty, output=output)
    with pytest.raises(ValueError, match='EVIDENCE_INVALID'):
        consume(runtime)
    assert len(calls) == 1
    assert not duty.calls and not store.has_source('reply:jev:1')


def test_delivered_reply_survives_optional_jev_failure_and_consumer_retry(tmp_path, monkeypatch):
    from runtime.personal_chat import backend
    duty = Duty(error='JEV_HTTP_503')
    store, runtime, _ = setup(tmp_path, monkeypatch, duty)
    row = {'state': 'SENT', 'content': USER, 'reply_text': REPLY, 'delivery_id': 'already-sent'}
    persisted = []
    async def commit(server, current):
        await runtime.consume_exchange('reply:jev:1', current['content'], current['reply_text'], occurred_at=NOW)
        current['daily_life_status'] = 'COMMITTED'
    async def persist(server):
        persisted.append(deepcopy(row))
    monkeypatch.setattr(backend, 'commit', commit)
    monkeypatch.setattr(backend, 'persist_chat', persist)
    server = SimpleNamespace(_safe_log=lambda *args, **kwargs: None)
    asyncio.run(backend.recoverable_commit(server, row))
    assert row['state'] == 'SENT' and row['reply_text'] == REPLY and row['delivery_id'] == 'already-sent'
    assert row['consumer_retry_at'] > 0 and not store.has_source('reply:jev:1')
    duty.error = None
    row.pop('consumer_retry_at')
    asyncio.run(backend.recoverable_commit(server, row))
    assert row['daily_life_status'] == 'COMMITTED' and 'consumer_error_code' not in row
    assert row['state'] == 'SENT' and event_count(store) == 1 and persisted


def test_full_old_episode_catalog_reserves_room_for_current_verified_experience(tmp_path, monkeypatch):
    port, requests = actual_port(monkeypatch, [candidate()])
    store, runtime, _ = setup(tmp_path, monkeypatch, port)
    for index in range(12):
        store.publish_day(f'day:old:{index}', {'location': '家里', 'activity': '摄影', 'note': f'练习第{index}种摄影构图。'}, [],
                          occurred_at=NOW - timedelta(days=13-index), activity_kind='creative')
    assert len(store.development_episodes(NOW)) == 12
    assert consume(runtime)
    episodes = requests[0][1]['episodes']
    assert len(episodes) == 12
    assert episodes[0] == {'episode_id': 'shared:photo', 'description': USER}
    assert episodes[1]['episode_id'] == 'daily:day:old:11'
    assert 'daily:day:old:0' not in [e['episode_id'] for e in episodes]
    assert event_count(store) == 1


def test_withdrawal_directory_selects_recent_twelve_valid_visible_records(tmp_path, monkeypatch):
    port, requests = actual_port(monkeypatch, [])
    store, runtime, _ = setup(tmp_path, monkeypatch, port, output={'updates': [], 'relationship': None})
    for index in range(14):
        seed(store, f'reply:old:{index}', NOW - timedelta(days=15-index))
    assert consume(runtime)
    rows = requests[0][1]['withdrawal_candidates']
    assert len(rows) == 12
    assert rows[0]['source_id'] == 'reply:old:13' and rows[-1]['source_id'] == 'reply:old:2'
    assert event_count(store) == 14  # bounded catalog selection never removes the ledger


def test_world_without_affect_quote_cannot_poison_next_exchange_withdrawal_catalog(tmp_path, monkeypatch):
    from runtime.private_world.character_emotion import CharacterEmotionStore
    from runtime.private_world.character_development import withdrawal_candidates
    note = '在窗边尝试不同的摄影构图。'
    world_candidate = {'source_id': 'day:no-affect', 'key': 'photography', 'stance': 'positive',
                       'quote': note, 'reason': '本次具体摄影构图体验。'}
    port, requests = actual_port(monkeypatch, lambda p: [world_candidate] if p['mode'] == 'world' else [candidate()])
    store, runtime, _ = setup(tmp_path, monkeypatch, port,
        output={'activity': {'kind': 'rest', 'place_id': 'home', 'focus': ''}, 'meal': None, 'project': None})
    store.configure_development(PERSONA)
    store.publish_day('day:no-affect', {'location': '家里', 'activity': '拍照', 'note': note}, [],
                      occurred_at=NOW - timedelta(hours=5), activity_kind='creative')
    emotion = CharacterEmotionStore(store.path)
    emotion.publish('day:no-affect')
    emotion.commit(emotion.assessment(['day:no-affect'], now=NOW), {'appraisals': [{
        'source_id': 'day:no-affect', 'quote': '', 'reaction': 'none', 'action_tendency': 'none',
        'goal_or_need': None, 'concern': None, 'revises': None, 'reported_affect': None}]})
    asyncio.run(runtime.refresh(NOW))
    assert runtime.error_code is None and event_count(store) == 1
    with store._db() as db:
        original = db.execute('SELECT payload FROM character_development_events').fetchone()[0]
        assert json.loads(original)['character_quote'] == ''
    async def extract(*args, **kwargs):
        return extraction()
    runtime._complete = extract
    later = NOW + timedelta(minutes=1)
    assert asyncio.run(runtime.consume_exchange('reply:after-no-affect', USER, REPLY, occurred_at=later))
    assert [kind for kind, _ in requests] == ['world', 'exchange']
    assert requests[-1][1]['withdrawal_candidates'] == []
    assert event_count(store) == 2
    with store._db() as db:
        assert db.execute("SELECT payload FROM character_development_events WHERE source_id='day:no-affect'").fetchone()[0] == original
        full = withdrawal_candidates(db, later, limit=None)
        assert any(row['source_id'] == 'day:no-affect' and row['character_quote'] == '' for row in full)
    # Missing prompt material is not loss of local withdrawal authority. New
    # evidenced corrections can still target the valid original through storage.
    user, reply = '更正，此前关于那次摄影体验的评价不准确。', '明白，我撤回那次摄影体验评价。'
    assert store.record_exchange('reply:direct-correction', user, reply, [], occurred_at=later + timedelta(minutes=1),
        development=[candidate(user_quote=user, character_quote=reply, experience_quote=user,
                               episode_id=None, withdraws='day:no-affect')])
    assert event_count(store) == 3
    assert store.development_view(later + timedelta(minutes=1))['items'][0]['source_ids'] == ['reply:after-no-affect']

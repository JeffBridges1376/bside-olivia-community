import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world import project_timing
from runtime.private_world.jev_world import decide
from runtime.private_world.world_decision import decision_context, compile_decision, LIFE_PROMPT
from tests.private_world.test_jev_world import Choices

SOURCE = datetime(2026, 9, 27, 18, 27, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 28, 8, tzinfo=timezone.utc)


def setup(store):
    quote = '明天七点前起床去上课。'
    store.record_exchange('reply:early:1', '早点休息。', quote, [dict(id='early', title='明天有课需要早起',
        detail=quote, status='planned', kind='linli', actor='linli', quote=quote)], occurred_at=SOURCE)


def context(store, now=NOW):
    snapshot = store.snapshot(now)
    return decision_context(dict(time=now.isoformat(), persona='[]', world=snapshot['world'],
        rhythm=snapshot['rhythm'], projects=store.exchange_state(now=now, include_history=True)['projects']))


def publish(store, data, *, scope='bounded', source_id='day:timing'):
    port = Choices(activity='rest_0_home', project='none', project_time_0_scope=scope,
                   project_time_0_day='0', project_time_0_hour='7', project_time_0_minute='0')
    result = asyncio.run(decide(port, data, LIFE_PROMPT))
    current, projects, meals = compile_decision(result, data)
    store.publish_day(source_id, current, projects, occurred_at=datetime.fromisoformat(data['time']),
                      activity_kind='rest', meals=meals, project_timing=result['project_timing'])
    return port, result


def test_source_clock_expiry_preserves_status_and_cache_survives_restart(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    setup(store)
    data = context(store)
    assert data['project_time_contexts']['p0']['source_local_time'].startswith('2026-09-28T02:27')
    port, _ = publish(store, data)
    assert len(port.calls) == 1
    project = DailyLifeStore(store.path).snapshot(NOW)['projects'][0]
    assert project['status'] == 'planned'
    assert project['deadline_at'] == '2026-09-27T23:00:00+00:00'
    assert project['deadline_expired'] is True
    assert project['quote'] == '明天七点前起床去上课。'
    before = store.snapshot(SOURCE + timedelta(minutes=1))['projects'][0]
    assert before['deadline_expired'] is False
    again = context(store, NOW + timedelta(hours=1))
    port = Choices(activity='rest_0_home', project='none')
    asyncio.run(decide(port, again, LIFE_PROMPT))
    assert not any(key.startswith('project_time_') for key in port.calls[0][1])
    assert 'existing_0' not in port.calls[0][1]['project']['criteria']


def test_thirteen_pending_projects_keep_catalog_and_advance_next_tick(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    quote = '明天晚上继续练习片段。'
    for index in range(13):
        store.record_exchange(f'reply:synthetic-{index}:1', '记得练习。', quote,
            [dict(id=f'p{index}', title=f'片段{index}', detail=quote, quote=quote,
                  status='planned', kind='linli', actor='linli')], occurred_at=SOURCE + timedelta(minutes=index))
    data = context(store)
    # Representative bounded observations remain in the decision duty.
    data['recent_observations'] = [{'note': f'观察{i}：' + '这一段的节奏还有不熟练的地方。' * 10} for i in range(4)]
    port = Choices(activity='rest_0_home', project='none')
    result = asyncio.run(decide(port, data, LIFE_PROMPT))
    state, questions, purpose = port.calls[0]
    assert len(port.calls) == 1 and len(state['context']['projects']) == 13
    deferred = state['project_timing_coverage']['deferred_project_indices']
    assert deferred and 0 < len(result['project_timing']) < 13
    assert all(f'existing_{index}' not in questions['project']['criteria'] for index in deferred)
    assert len(json.dumps(dict(state=state, questions=questions, purpose=purpose), ensure_ascii=False,
                          separators=(',', ':')).encode()) <= 30000
    current, projects, meals = compile_decision(result, data)
    store.publish_day('day:budget-first', current, projects, occurred_at=NOW, activity_kind='rest',
                      meals=meals, project_timing=result['project_timing'])
    first = {item['id'] for item in result['project_timing']}
    next_data = context(store, NOW + timedelta(minutes=31))
    next_port = Choices(activity='rest_0_home', project='none')
    next_result = asyncio.run(decide(next_port, next_data, LIFE_PROMPT))
    assert len(next_port.calls) == 1
    assert {item['id'] for item in next_result['project_timing']} - first
    assert all(item['id'] not in first for item in next_result['project_timing'])
    assert len(store.exchange_state(now=NOW + timedelta(minutes=31))['projects']) == 13


@pytest.mark.parametrize('old_scope', ['unclear', 'bounded', 'open'])
def test_contract_revision_reconsiders_old_uncertainty_only(tmp_path, old_scope):
    store = DailyLifeStore(tmp_path / 'world.db')
    setup(store)
    project = context(store)['projects'][0]
    legacy = dict(id=project['id'], version=project_timing.source_version(project), scope=old_scope,
                  deadline_at='2026-09-27T23:00:00+00:00' if old_scope == 'bounded' else None,
                  source_at=project['updated_at'])
    with store._db() as db:
        db.execute('INSERT INTO life_project_timing VALUES (?,?)', (legacy['version'], json.dumps(legacy)))
    data = context(store)
    if old_scope in {'unclear', 'open'}:
        assert data['projects'][0]['time_scope_pending']
        assert project_timing.pending(data['projects'])
        assert 'inherit' not in project_timing.questions(data)['project_time_0_scope']['criteria']
        publish(store, data)
        assert not project_timing.pending(context(store)['projects'])
    else:
        assert not project_timing.pending(data['projects'])
        assert data['projects'][0]['time_scope'] == old_scope
        assert data['projects'][0]['deadline_expired'] == (old_scope == 'bounded')


def test_source_context_preserves_sleep_farewell_and_exact_revision(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    setup(store)
    quote = '明天七点前起床去上课。'
    original = '今天就说到这里。' + quote + '先把手机放在床头，闭眼睡吧。晚安。'
    row = dict(letter_id='early', reply_revision=1, letter_status='COMPLETED', private_world_status='COMMITTED',
               private_world_occurred_at=SOURCE.isoformat(), reply_text=original, content='用户原话不应被拼入。')
    data = project_timing.attach_source_context(context(store), [row])
    assert 'project_time_calendars' not in data
    assert data['project_time_contexts']['p0']['source_local_time'].startswith('2026-09-28T02:27')
    source = data['project_source_contexts']['reply:early:1']
    assert source['coverage'] == 'complete'
    assert source['segments'][0]['text'] == original
    assert '用户原话' not in json.dumps(source, ensure_ascii=False)
    port = Choices(activity='rest_0_home', project='none')
    asyncio.run(decide(port, data, LIFE_PROMPT))
    sent = port.calls[0][0]['context']
    assert 'project_source_contexts' not in sent
    assert sent['project_time_contexts']['p0']['reply_text'] == original
    assert not project_timing.attach_source_context(context(store), [{**row, 'reply_revision': 2}])['project_source_contexts']
    assert not project_timing.attach_source_context(context(store), [{**row, 'letter_status': 'GENERATING'}])['project_source_contexts']
    normal_text = quote + '中间没有时间信息。' * 40 + '放下手机，闭眼睡吧。晚安。'
    normal = project_timing.attach_source_context(context(store), [{**row, 'reply_text': normal_text}])
    assert normal['project_source_contexts']['reply:early:1']['coverage'] == 'complete'
    long_text = quote + '中间没有时间信息。' * 180 + '放下手机，闭眼睡吧。晚安。'
    data = project_timing.attach_source_context(context(store), [{**row, 'reply_text': long_text}])
    source = data['project_source_contexts']['reply:early:1']
    assert source['coverage'] == 'partial' and len(source['segments']) == 2
    assert sum(len(segment['text']) for segment in source['segments']) <= 240
    assert source['segments'][0]['end'] < source['segments'][1]['start']
    for segment in source['segments']:
        assert long_text[segment['start']:segment['end']] == segment['text']


@pytest.mark.parametrize('scope,minute,stage', [('unclear', '0', 'scope_unclear'),
                                             ('bounded', 'unknown', 'deadline_fields_unknown')])
def test_unclear_diagnostic_distinguishes_scope_from_missing_clock_without_original_text(tmp_path, scope, minute, stage):
    store = DailyLifeStore(tmp_path / 'world.db')
    setup(store)
    data = context(store)
    port = Choices(activity='rest_0_home', project='none', project_time_0_scope=scope,
                   project_time_0_day='0', project_time_0_hour='7', project_time_0_minute=minute)
    result = asyncio.run(decide(port, data, LIFE_PROMPT))
    item = result['project_timing'][0]
    assert item['scope'] == 'unclear'
    assert item['diagnostic']['scope'] == scope and item['diagnostic']['stage'] == stage
    assert item['diagnostic']['missing_fields'] == (['minute'] if scope == 'bounded' else [])
    assert data['projects'][0]['quote'] not in json.dumps(item['diagnostic'], ensure_ascii=False)
    current, projects, meals = compile_decision(result, data)
    store.publish_day('day:diagnostic', current, projects, occurred_at=NOW, activity_kind='rest',
                      meals=meals, project_timing=result['project_timing'])
    with store._db() as db:
        stored = json.loads(db.execute('SELECT payload FROM life_project_timing WHERE version=?', (item['version'],)).fetchone()[0])
    assert stored['diagnostic'] == item['diagnostic']
    assert not project_timing.pending(context(store)['projects'])  # No automatic same-contract retry.


@pytest.mark.parametrize('scope', ['unclear', 'open', 'bounded'])
@pytest.mark.parametrize('revision', [2, 3])
def test_v4_reconsiders_old_open_once_and_preserves_deadlines(tmp_path, scope, revision):
    import hashlib
    store = DailyLifeStore(tmp_path / 'world.db')
    setup(store)
    project = context(store)['projects'][0]
    source_version = project_timing.source_version(project)
    legacy = dict(id=project['id'], version=hashlib.sha256(f'{revision}:{source_version}'.encode()).hexdigest(),
        scope=scope, deadline_at='2026-09-27T23:00:00+00:00' if scope == 'bounded' else None,
        source_at=project['updated_at'], source_version=source_version, contract_revision=revision)
    with store._db() as db:
        db.execute('INSERT INTO life_project_timing VALUES (?,?)', (legacy['version'], json.dumps(legacy)))
    data = context(store)
    assert bool(project_timing.pending(data['projects'])) == (scope in {'unclear', 'open'})
    if scope in {'unclear', 'open'}:
        publish(store, data, scope=scope)
        assert not project_timing.pending(context(store)['projects'])


@pytest.mark.parametrize('scope', ['open', 'unclear'])
def test_long_term_or_uncertain_time_never_automatically_expires(tmp_path, scope):
    store = DailyLifeStore(tmp_path / 'world.db')
    setup(store)
    publish(store, context(store), scope=scope)
    project = store.snapshot(NOW + timedelta(days=30))['projects'][0]
    assert project['status'] == 'planned'
    assert project['deadline_at'] is None and project['deadline_expired'] is False
    assert project['time_scope'] == scope


def test_changed_source_reuses_absolute_deadline_unless_explicitly_reinterpreted(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    setup(store)
    publish(store, context(store))
    later = NOW + timedelta(hours=1)
    store.record_exchange('reply:later:1', '记得安排。', '这个安排还保留。', [dict(id='early', title='明天有课需要早起',
        detail='这个安排还保留。', status='planned', kind='linli', actor='linli', quote='这个安排还保留。')], occurred_at=later)
    data = context(store, later)
    assert data['projects'][0]['previous_time_scope']['deadline_at'] == '2026-09-27T23:00:00+00:00'
    assert data['projects'][0]['deadline_expired'] is True
    assert data['projects'][0]['time_scope_pending'] is True
    assert 'time_scope' not in data['projects'][0]
    port, _ = publish(store, data, scope='inherit', source_id='day:later')
    assert len(port.calls) == 1
    project = store.snapshot(later)['projects'][0]
    assert project['deadline_expired'] and project['deadline_at'] == '2026-09-27T23:00:00+00:00'


def test_new_deadline_in_same_packet_never_autocompletes_expired_project(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    setup(store)
    data = context(store)
    port = Choices(activity='practice_0_home', project='existing_0', project_outcome='completed',
                   project_time_0_scope='bounded', project_time_0_day='0', project_time_0_hour='7', project_time_0_minute='0')
    result = asyncio.run(decide(port, data, LIFE_PROMPT))
    assert result['project'] is None
    assert result['project_timing'][0]['scope'] == 'bounded'


def test_transient_observation_is_not_progressed_but_long_term_project_remains(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    for identity, quote in [('class', '在的，正上着课'), ('chopin', '长期练习肖邦夜曲')]:
        store.record_exchange(f'reply:{identity}:1', '现在呢？', quote,
            [dict(id=identity, title=quote, detail=quote, quote=quote,
                  status='ongoing', kind='linli', actor='linli')], occurred_at=SOURCE)
    data = context(store)
    indices = {p['id']: index for index, p in enumerate(data['projects'])}
    choices = {f'project_time_{index}_scope': 'transient' if identity == 'class' else 'open'
               for identity, index in indices.items()}
    port = Choices(activity='practice_0_home', project=f"existing_{indices['class']}", **choices)
    result = asyncio.run(decide(port, data, LIFE_PROMPT))
    assert result['project'] is None
    current, projects, meals = compile_decision(result, data)
    store.publish_day('day:transient', current, projects, occurred_at=NOW, activity_kind='practice',
                      meals=meals, project_timing=result['project_timing'])
    projects = store.snapshot(NOW)['projects']
    assert [p['id'] for p in projects] == ['chopin', 'class']
    assert projects[1]['status'] == 'ongoing' and projects[1]['deadline_at'] is None
    assert projects[1]['time_scope'] == 'transient' and projects[0]['time_scope'] == 'open'
    again = context(store)
    again_port = Choices(activity='rest_0_home', project='none')
    asyncio.run(decide(again_port, again, LIFE_PROMPT))
    questions = again_port.calls[0][1]
    assert not any(k.startswith('project_time_') for k in questions)
    for index, item in enumerate(again['projects']):
        assert (f'existing_{index}' in questions['project']['criteria']) == (item['id'] == 'chopin')


def test_timing_publication_uses_identity_not_decision_catalog_order(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    setup(store)
    quote = '持续练习钢琴。'
    store.record_exchange('reply:practice:1', '继续练习。', quote,
        [dict(id='practice', title='练习钢琴', detail=quote, quote=quote,
              status='planned', kind='linli', actor='linli')], occurred_at=SOURCE + timedelta(minutes=1))
    data = context(store)
    with store._db() as db:
        storage_ids = [item['id'] for item in store._projects_at(db, NOW)]
    data['projects'] = sorted(data['projects'], key=lambda item: storage_ids.index(item['id']), reverse=True)
    port = Choices(activity='rest_0_home', project='none',
                   project_time_0_scope='open', project_time_1_scope='open')
    result = asyncio.run(decide(port, data, LIFE_PROMPT))
    assert [item['id'] for item in data['projects']] == list(reversed(storage_ids))
    current, projects, meals = compile_decision(result, data)
    store.publish_day('day:reordered', current, projects, occurred_at=NOW, activity_kind='rest',
                      meals=meals, project_timing=result['project_timing'])
    assert store.has_source('day:reordered')
    with store._db() as db:
        stored = [json.loads(row[0]) for row in db.execute('SELECT payload FROM life_project_timing')]
    for item in stored:
        index = item['diagnostic']['project_index']
        assert data['projects'][index]['id'] == item['id']
        assert storage_ids[index] != item['id']
    for field in ('id', 'version', 'source_version'):
        forged = [{**result['project_timing'][0], field: 'forged'}]
        with pytest.raises(ValueError, match='DAILY_LIFE_PROJECT_TIMING_INVALID'):
            project_timing.validate(forged, data)


def test_timing_version_forgery_rolls_back_world_publication(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    setup(store)
    data = context(store)
    timing = dict(id='early', version='forged', source_at=SOURCE.isoformat(), scope='open', deadline_at=None)
    with pytest.raises(ValueError, match='DAILY_LIFE_PROJECT_TIMING_INVALID'):
        store.publish_day('day:invalid', dict(location='家里', activity='休息', note='休息。'), [],
                          occurred_at=NOW, project_timing=[timing])
    assert not store.has_source('day:invalid')

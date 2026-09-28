import asyncio
import json
from datetime import datetime, timezone

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.reply.world_context_selection import select_world_context, WorldSelectionError


def test_selection_dialogue_keeps_recent_pair_provenance_without_repeating_full_prompt():
    from types import SimpleNamespace
    from runtime.reply.world_context_selection import selection_dialogue
    rows = [{'source_id': f'r{i}', 'user_letter': f'u{i}', 'linli_reply': f'a{i}',
             'sent_at': f't{i}', 'truncated': False} for i in range(4)]
    result = selection_dialogue([SimpleNamespace(fragment_id='chat.recent',
        text=json.dumps({'meaning': 'LONG_REPEATED_CONTRACT', 'letters': rows})),
        SimpleNamespace(fragment_id='chat.historical', text='OLDER_RETRIEVAL')])
    assert result['exchanges'] == rows[-2:]
    assert result['coverage'] == 'last_two_exchanges_only'
    assert 'LONG_REPEATED_CONTRACT' not in json.dumps(result)
    assert 'OLDER_RETRIEVAL' not in json.dumps(result)


def test_jev_can_select_complete_timetable_without_current_class(tmp_path):
    now = datetime(2026, 9, 28, 5, tzinfo=timezone.utc)
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=now)
    class Port:
        async def ask(self, state, questions, *, purpose):
            assert purpose == 'reply-world-selection'
            return {key: 'rank9' if item['field'] == 'schedule' else 'rank0'
                    for key, item in state['records'].items()}
    result = json.loads(asyncio.run(select_world_context(Port(), packet, '你下午不用去学校吗？')))
    assert result['schedule']['current_class'] is None
    assert result['schedule']['next_class']['start'].startswith('2026-09-28T14:00')
    assert len(result['schedule']['classes']) == 2
    assert result['as_of'] == now.isoformat()
    assert 'character_development' not in result


def test_selection_budget_does_not_choose_manual_priority_or_truncate(tmp_path):
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime.now(timezone.utc))
    class Port:
        async def ask(self, state, questions, **kwargs):
            return {key: 'rank5' for key in questions}
    with pytest.raises(WorldSelectionError, match='JEV_WORLD_SELECTION_BUDGET'):
        asyncio.run(select_world_context(Port(), packet, '说说今天', max_chars=100))


def test_failed_jev_does_not_fall_back_to_keyword_selection(tmp_path):
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime.now(timezone.utc))
    class Port:
        async def ask(self, *args, **kwargs):
            raise RuntimeError('PRIVATE_PROVIDER_DETAILS')
    with pytest.raises(WorldSelectionError, match='^JEV_WORLD_SELECTION_UNAVAILABLE$'):
        asyncio.run(select_world_context(Port(), packet, '学校'))


@pytest.mark.parametrize('code', ['JEV_HTTP_503', 'JEV_BILLING_UNAVAILABLE',
    'JEV_BILLING_RECEIPT_INVALID', 'JEV_RESPONSE_INVALID'])
def test_selection_preserves_safe_cause_through_chat_mapping(tmp_path, code):
    from runtime.personal_chat.backend import _generation_failure_code
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime.now(timezone.utc))
    class Port:
        async def ask(self, *args, **kwargs):
            raise ValueError(code)
    with pytest.raises(WorldSelectionError, match='^' + code + '$'):
        asyncio.run(select_world_context(Port(), packet, '晚饭呢'))
    assert _generation_failure_code(code) == code
    assert _generation_failure_code('JEV_WORLD_SELECTION_UNAVAILABLE') == 'JEV_WORLD_SELECTION_UNAVAILABLE'


def test_candidate_projection_uses_as_of_not_future_world(tmp_path):
    now = datetime(2026, 9, 28, 5, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    store.publish_day('future', {'location': '家里', 'activity': '练琴', 'note': 'FUTURE_SECRET'}, [],
                      occurred_at=datetime(2026, 9, 29, 5, tzinfo=timezone.utc))
    assert 'FUTURE_SECRET' not in json.dumps(store.reply_candidates(now=now))


def test_jev_priorities_keep_whole_timetable_when_related_records_exceed_budget(tmp_path):
    now = datetime(2026, 9, 28, 5, tzinfo=timezone.utc)
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=now)
    packet['records'] = [
        {'field': 'recent_episodes', 'many': True, 'value': {'note': '练习经过' * 900}},
        *packet['records'],
    ]
    calls = []
    class Port:
        async def ask(self, state, questions, **kwargs):
            calls.append(state)
            assert all('incremental_chars' not in r and 'value' not in r for r in state['records'].values())
            return {key: 'rank9' if r['field'] == 'schedule' else 'rank3'
                    for key, r in state['records'].items()}
    result = json.loads(asyncio.run(select_world_context(Port(), packet, '下午的课呢？', max_chars=1600)))
    assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) <= 1600
    assert len(result['schedule']['classes']) == 2
    assert result['schedule']['next_class']['start'].startswith('2026-09-28T14:00')
    assert 'recent_episodes' not in result
    assert len(calls) == 1


def test_later_exchange_marks_old_published_activity_historical(tmp_path):
    from datetime import timedelta
    now = datetime(2026, 9, 28, 5, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    store.publish_day('day:past', {'location': '家里', 'activity': '休息', 'note': '在家休息。'}, [],
                      occurred_at=now-timedelta(minutes=5))
    store.record_exchange('reply:new', '你好', '你好', [], occurred_at=now)
    packet = store.reply_candidates(now=now)
    assert packet['base']['stale'] is True
    assert not any(r['field'] == 'current' for r in packet['records'])
    last = next(r['value'] for r in packet['records'] if r['field'] == 'last_observation')
    assert last['evidence_kind'] == 'published_life' and last['actor'] == 'linli'


def test_batches_measure_questions_and_state_together_against_transport_cap(tmp_path):
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime.now(timezone.utc))
    packet['records'] = [{'field': 'threads', 'many': True, 'value': {'note': '内容' * 400}}
                         for _ in range(25)]
    calls = []
    packets = []
    class Port:
        async def ask(self, state, questions, *, purpose):
            envelope = {'state': state, 'questions': questions, 'purpose': purpose}
            assert len(json.dumps(envelope, ensure_ascii=False, separators=(',', ':')).encode()) <= 30000
            calls.extend(questions)
            packets.append(envelope)
            return {key: 'rank0' for key in questions}
    asyncio.run(select_world_context(Port(), packet, '你好'))
    assert len(calls) == 25 and len(set(calls)) == 25
    assert len(packets) == 1


def test_directory_preview_never_replaces_selected_complete_evidence(tmp_path):
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime.now(timezone.utc))
    original = {'note': '完整生活经过。' * 100, 'status': 'completed', 'title': '练习'}
    packet['records'] = [{'field': 'threads', 'many': True, 'value': original}]
    class Port:
        async def ask(self, state, questions, **kwargs):
            assert state['records']['r0'] == {'field': 'threads', 'status': 'completed', 'title': '练习'}
            return {'r0': 'rank9'}
    result = json.loads(asyncio.run(select_world_context(Port(), packet, '练习怎么样？')))
    assert result['threads'] == [original]


def test_short_and_long_records_use_same_metadata_directory_and_restore_full_sources(tmp_path):
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime.now(timezone.utc))
    originals = [dict(id='p1', title='明天的课', actor='linli', status='cancelled',
                      updated_at='2026-09-28T01:00:00+00:00', detail='SHORT_PRIVATE_DETAIL'),
                 dict(id='p2', title='明天的课', actor='user', status='planned',
                      updated_at='2026-09-28T02:00:00+00:00', detail='LONG_PRIVATE_DETAIL' * 200)]
    packet['records'] = [{'field': 'threads', 'many': True, 'value': item} for item in originals]
    packet['rhythm']['internal_history'] = 'RHYTHM_PRIVATE_DETAIL' * 500
    class Port:
        async def ask(self, state, questions, **kwargs):
            encoded = json.dumps(state)
            assert 'PRIVATE_DETAIL' not in encoded and 'preview' not in encoded
            assert state['records']['r0']['status'] == 'cancelled'
            assert state['records']['r1']['actor'] == 'user'
            assert state['records']['r0']['updated_at'] != state['records']['r1']['updated_at']
            return {'r0': 'rank9', 'r1': 'rank8'}
    result = json.loads(asyncio.run(select_world_context(Port(), packet, '刚才说的课程怎么回事？', max_chars=10000)))
    assert result['threads'] == originals


def test_directory_uses_short_same_source_aliases_and_host_restores_identifiers(tmp_path):
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime.now(timezone.utc))
    source_id = 'reply:' + 'a' * 64
    originals = [dict(id='opaque-' + 'b' * 64, source_id=source_id, title='下午练习', actor='linli', status=status)
                 for status in ('planned', 'cancelled')]
    packet['records'] = [{'field': 'threads', 'many': True, 'value': item} for item in originals]
    class Port:
        async def ask(self, state, questions, **kwargs):
            assert source_id not in json.dumps(state) and 'opaque-' not in json.dumps(state)
            first, second = state['records']['r0'], state['records']['r1']
            assert first['source_id'] == second['source_id'] == 's0'
            assert first['id'] == second['id'] == 'i0'
            assert first['status'] == 'planned' and second['status'] == 'cancelled'
            return {'r0': 'rank8', 'r1': 'rank9'}
    result = json.loads(asyncio.run(select_world_context(Port(), packet, '还去练习吗？')))
    assert result['threads'] == list(reversed(originals))

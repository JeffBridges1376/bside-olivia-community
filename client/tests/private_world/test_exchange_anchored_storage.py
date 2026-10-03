"""Bound evidence must survive the real store; all JEV answers are synthetic."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.jev_exchange import extract


NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


class AnchoredPort:
    def __init__(self, selections):
        self.selections = selections
        self.calls = []

    async def ask(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        answers = {key: 'none' if 'none' in question['criteria'] else next(iter(question['criteria']))
                   for key, question in questions.items()}
        for key, question in questions.items():
            if not key.startswith('update_'):
                continue
            _, index, field = key.split('_', 2)
            if int(index) >= len(self.selections):
                continue
            action, identity, status, evidence = self.selections[int(index)]
            if field == 'quote':
                answers[key] = next(ref for ref, anchor in state['action_anchors'].items()
                                    if anchor['text'] == action)
            elif field == 'identity':
                answers[key] = identity
            elif field == 'existing_match':
                answers[key] = identity if identity.startswith('p') else 'none'
            elif field == 'applicability_kind':
                if identity == 'none':
                    answers[key] = 'none'
                elif identity.startswith('new_'):
                    answers[key] = identity.removeprefix('new_')
                else:
                    previous = self.calls[0][0]['exchange']['previous_state']
                    projects = [item for group in ('projects', 'shared') for item in previous.get(group, [])]
                    answers[key] = projects[int(identity[1:])].get('kind', 'linli')
            elif field == 'status':
                answers[key] = status
            elif field == 'evidence':
                if evidence is None:
                    answers[key] = 'none'
                else:
                    answers[key] = next(ref for ref in question['criteria'] if ref != 'none'
                        and state['sources']['user_letter' if ref.startswith('u') else 'linli_reply'][
                            slice(*state['quotes'][ref])] == evidence)
        return answers


def extracted(reply, selections, previous=None):
    port = AnchoredPort(selections)
    payload = asyncio.run(extract(port, {'user_letter': '好。', 'linli_reply': reply,
                                        'previous_state': previous or {}}, '', 'life:storage'))
    return payload, port


def test_completed_independent_action_after_planned_prefix_commits_and_survives_restart(tmp_path):
    reply = '我准备明天寄书，碗已经洗好了。'
    payload, port = extracted(reply, [('碗已经洗好了。', 'new_linli', 'completed', '碗已经洗好了。')])
    path = tmp_path / 'life.sqlite3'
    store = DailyLifeStore(path)
    legacy_support_quote = [{**payload['updates'][0], 'quote': reply}]
    with pytest.raises(ValueError, match='DAILY_LIFE_PHASE_CONFLICT'):
        store.record_exchange('reply:legacy-mixed:1', '好。', reply, legacy_support_quote, occurred_at=NOW)
    assert not store.has_source('reply:legacy-mixed:1')
    assert store.record_exchange('reply:mixed:1', '好。', reply, payload['updates'], occurred_at=NOW)
    assert payload['updates'][0]['detail'] == reply
    assert len(port.calls) == 2
    restored, = DailyLifeStore(path).exchange_state(reply, now=NOW)['projects']
    assert restored['status'] == 'completed'
    assert restored['quote'] == '碗已经洗好了。'
    assert restored['source_id'] == 'reply:mixed:1'
    assert not store.record_exchange('reply:mixed:1', '好。', reply, payload['updates'], occurred_at=NOW)


def test_existing_canonical_title_does_not_have_to_equal_current_action_anchor(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    earlier = '我现在正在洗碗。'
    store.record_exchange('reply:earlier:1', '好。', earlier, [dict(
        id='dishes', title='洗碗', detail=earlier, status='ongoing', kind='linli', actor='linli',
        quote=earlier)], occurred_at=NOW)
    reply = '我准备明天寄书，碗已经洗好了。'
    previous = {'projects': [{'id': 'dishes', 'title': '洗碗', 'kind': 'linli', 'status': 'ongoing'}]}
    payload, _ = extracted(reply, [('碗已经洗好了。', 'p0', 'completed', '碗已经洗好了。')], previous)
    store.record_exchange('reply:existing:1', '好。', reply, payload['updates'],
                          occurred_at=NOW + timedelta(minutes=1))
    restored, = store.exchange_state(reply, now=NOW + timedelta(minutes=1))['projects']
    assert (restored['id'], restored['title'], restored['status']) == ('dishes', '洗碗', 'completed')
    assert restored['quote'] == '碗已经洗好了。'


def test_planned_and_completed_actions_in_same_support_keep_their_own_phase(tmp_path):
    reply = '我准备明天寄书，碗已经洗好了。'
    payload, _ = extracted(reply, [
        ('我准备明天寄书，', 'new_shared', 'planned', '我准备明天寄书，'),
        ('碗已经洗好了。', 'new_linli', 'completed', '碗已经洗好了。')])
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    store.record_exchange('reply:separate:1', '好。', reply, payload['updates'], occurred_at=NOW)
    state = store.exchange_state(reply, now=NOW)
    assert state['shared'][0]['status'] == 'planned'
    assert state['shared'][0]['quote'] == '我准备明天寄书，'
    assert state['projects'][0]['status'] == 'completed'
    assert state['projects'][0]['quote'] == '碗已经洗好了。'
    assert all(update['detail'] == reply for update in payload['updates'])


def test_condition_is_retained_in_bound_evidence_without_certifying_future_completion(tmp_path):
    reply = '如果明天不下雨，我会寄书。'
    payload, port = extracted(reply, [('我会寄书。', 'new_shared', 'planned', reply)])
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    store.record_exchange('reply:condition:1', '好。', reply, payload['updates'], occurred_at=NOW)
    restored, = store.exchange_state(now=NOW)['shared']
    assert restored['status'] == 'planned'
    assert restored['quote'] == reply
    evidence_question = port.calls[1][1]['update_0_evidence']
    assert reply in evidence_question['instructions']
    assert port.calls[1][0]['sources']['linli_reply'] == reply


def test_paused_existing_action_keeps_full_evidence_across_future_continuation_sentence(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    earlier = '我正在写小提琴曲。'
    store.record_exchange('reply:music:earlier', '好。', earlier, [dict(
        id='music', title='写小提琴曲', detail=earlier, status='ongoing', kind='linli',
        actor='linli', quote=earlier)], occurred_at=NOW)
    reply = '小提琴曲这几天先停一下。等我下周再接着写。'
    previous = {'projects': [{'id': 'music', 'title': '写小提琴曲', 'kind': 'linli', 'status': 'ongoing'}]}
    payload, _ = extracted(reply, [('小提琴曲这几天先停一下。', 'p0', 'paused', reply)], previous)
    store.record_exchange('reply:music:pause', '好。', reply, payload['updates'],
                          occurred_at=NOW + timedelta(minutes=1))
    restored, = DailyLifeStore(store.path).exchange_state(reply, now=NOW + timedelta(minutes=1))['projects']
    assert (restored['id'], restored['title'], restored['status']) == ('music', '写小提琴曲', 'paused')
    assert restored['quote'] == reply


def test_future_conditional_evidence_across_sentences_remains_planned_and_guard_stays_active(tmp_path):
    reply = '我准备明天修相机。如果下雨，就改到周末。'
    payload, _ = extracted(reply, [('我准备明天修相机。', 'new_linli', 'planned', reply)])
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    with pytest.raises(ValueError, match='DAILY_LIFE_PHASE_CONFLICT'):
        store.record_exchange('reply:conditional:invalid', '好。', reply,
                              [{**payload['updates'][0], 'status': 'completed'}], occurred_at=NOW)
    assert not store.has_source('reply:conditional:invalid')
    store.record_exchange('reply:conditional:planned', '好。', reply, payload['updates'], occurred_at=NOW)
    restored, = DailyLifeStore(store.path).exchange_state(reply, now=NOW)['projects']
    assert restored['status'] == 'planned'
    assert restored['quote'] == reply


@pytest.mark.parametrize('quote', ['我准备明天洗碗。', '我打算明天洗碗。',
                                   '我还没洗碗。', '尚未洗碗。', '暂未洗碗。'])
@pytest.mark.parametrize('actor', ['user', 'linli'])
def test_existing_future_and_negative_completed_guard_still_rejects_without_writes(tmp_path, quote, actor):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    update = dict(id='dishes', title='洗碗', detail=quote, status='completed',
                  kind='shared', actor=actor, quote=quote)
    with pytest.raises(ValueError, match='DAILY_LIFE_PHASE_CONFLICT'):
        store.record_exchange('reply:invalid:1', quote if actor == 'user' else '好。',
                              quote if actor == 'linli' else '好。', [update], occurred_at=NOW)
    assert not store.has_source('reply:invalid:1')
    assert store.exchange_state(now=NOW)['shared'] == []


def test_missing_bound_evidence_does_not_fall_back_to_an_unqualified_action(tmp_path):
    reply = '如果明天不下雨，我会寄书。'
    payload, _ = extracted(reply, [('我会寄书。', 'new_shared', 'completed', None)])
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    store.record_exchange('reply:no-proof:1', '好。', reply, payload['updates'], occurred_at=NOW)
    assert payload['updates'] == []
    assert store.exchange_state(now=NOW)['shared'] == []

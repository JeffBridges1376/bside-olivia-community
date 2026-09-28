"""Small classifier context retains durable requirements and source evidence."""
import pytest

from runtime.reply import companion_runtime as runtime
from runtime.personal_chat.context import READ_WINDOW
from runtime.reply.companion_decision import FrozenCompanionDecision, FrozenCompanionTurn, CompanionDecisionError
from tests.persona.test_companion_decision import envelope, input_args


def frames(*ids):
    return [dict(source=f'reply:{key}:1', event_id=f'reply:{key}:1:{actor}',
                 role=role, text=f'{key}-{actor}', image_delivery_confirmed=role == 'assistant')
            for key in ids for actor, role in [('user', 'user'), ('linli', 'assistant')]]


def test_legacy_pending_originals_retained_but_old_assistant_history_removed(monkeypatch):
    monkeypatch.setattr(runtime, '_recent_dialogue', lambda _: frames('old', 'recent'))
    rows = runtime._decision_context([])
    assert [r['text'] for r in rows] == ['old-user', 'recent-user', 'recent-linli']
    assert rows[-1]['image_delivery_confirmed'] is True


def test_latest_decision_carries_pending_original_outside_writer_window(monkeypatch):
    monkeypatch.setattr(runtime, '_recent_dialogue', lambda _: frames('recent'))
    window = [dict(letter_id='old', content='下次先语音，再给两张图'),
              dict(letter_id='recent', content='你好', companion_decision=dict(
                  source_id_map={'t1': 'reply:old:user'},
                  plan={'understanding': {'requirements': [dict(fulfillment='pending', evidence_turn_ids=['t1'])]}}))]
    token = READ_WINDOW.set(window)
    try:
        rows = runtime._decision_context([])
    finally:
        READ_WINDOW.reset(token)
    assert rows[0]['text'] == window[0]['content']
    assert rows[0]['event_id'] == 'reply:old:user'
    assert len(rows) == 3


def test_completed_history_removed_but_explicit_reference_keeps_original_pair(monkeypatch):
    monkeypatch.setattr(runtime, '_recent_dialogue', lambda _: frames('old', 'recent'))
    token = READ_WINDOW.set([dict(letter_id='old'), dict(letter_id='recent', companion_decision=dict(
        source_id_map={}, plan={'understanding': {'requirements': []}}))])
    try:
        assert len(runtime._decision_context([])) == 2
        rows = runtime._decision_context([], ['reply:old:user'])
        assert [r['text'] for r in rows] == ['old-user', 'old-linli', 'recent-user', 'recent-linli']
        with pytest.raises(runtime.CompanionRuntimeError, match='CONTEXT_UNAVAILABLE'):
            runtime._decision_context([], ['reply:missing:user'])
    finally:
        READ_WINDOW.reset(token)


def test_single_coverage_record_cannot_be_replayed_as_full():
    turn = FrozenCompanionTurn.create(**input_args())
    value = envelope()
    value['evaluation'] = dict(profile='single_delivery', not_evaluated=['control'])
    decision = FrozenCompanionDecision.from_response(turn, value)
    assert decision.record()['evaluation'] == value['evaluation']
    assert FrozenCompanionDecision.from_record(turn, decision.record(), profile='single_delivery') == decision
    with pytest.raises(CompanionDecisionError):
        FrozenCompanionDecision.from_record(turn, decision.record())
    value['evaluation']['profile'] = 'mystery'
    with pytest.raises(CompanionDecisionError):
        FrozenCompanionDecision.from_response(turn, value)


def test_legacy_complete_record_compatible_without_invented_coverage():
    turn = FrozenCompanionTurn.create(**input_args())
    decision = FrozenCompanionDecision.from_response(turn, envelope())
    assert 'evaluation' not in decision.record()
    assert FrozenCompanionDecision.from_record(turn, decision.record(), profile='single_delivery').profile == 'full'

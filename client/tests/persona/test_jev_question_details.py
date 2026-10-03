"""Choice metadata survives the sidecar without inventing certainty for old answers."""
import asyncio
from copy import deepcopy
from email.message import Message
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.reply import jev_questions
from runtime.reply.jev_semantic_service import decide


QUESTIONS = {'action': {'instructions': 'Choose an action',
                        'criteria': {'rest': 'Rest', 'continue': 'Continue'}}}
DETAIL = {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .2},
          'confidence': .6, 'confidence_source': 'upstream'}


def _packet(questions=None):
    return {'state': {'text': 'synthetic'}, 'questions': questions or deepcopy(QUESTIONS),
            'purpose': 'synthetic'}


def _port(monkeypatch, detail, *, decisions=None):
    calls, settlements = [], []
    monkeypatch.setattr(jev_questions, 'settle_receipt_sync',
                        lambda billing, digest: settlements.append((billing, digest)))

    class Response:
        status = 200
        headers = Message()
        headers['Content-Type'] = 'application/json'

        def __init__(self, value):
            self.raw = json.dumps(value).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, limit):
            return self.raw[:limit]

    class Opener:
        def open(self, request, **_):
            calls.append(request)
            value = {'backend': 'jev', 'input_digest': hashlib.sha256(request.data).hexdigest(),
                     'decisions': decisions if decisions is not None else {'action': 'rest'},
                     'billing': {'receipt': 'synthetic'}}
            if detail is not None:
                value['decision_details'] = deepcopy(detail)
            return Response(value)

    port = jev_questions.JevQuestionsPort('http://127.0.0.1:8097/v1/companion/decide')
    port.transport._opener = Opener()
    return port, calls, settlements


@pytest.mark.parametrize('confidence', [None, .6])
def test_sidecar_keeps_native_distribution_and_valid_confidence(confidence):
    answer = {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .2}}
    if confidence is not None:
        answer['confidence'] = confidence
    native = SimpleNamespace(ask=lambda *_: {'action': answer})
    result = decide(native, _packet())
    assert result['decisions'] == {'action': 'rest'}
    detail = result['decision_details']['action']
    assert detail['choice'] == 'rest' and detail['probabilities'] == answer['probabilities']
    assert detail['confidence'] == pytest.approx(.6)
    assert detail['confidence_source'] == ('upstream' if confidence is not None else 'probability-derived')


def test_singleton_is_deterministic_without_calling_native_provider():
    packet = _packet({'action': {'instructions': 'Only possible value', 'criteria': {'rest': 'Rest'}}})
    result = decide(SimpleNamespace(), packet)
    assert result['decisions'] == {'action': 'rest'}
    assert result['decision_details'] == {'action': {'choice': 'rest', 'probabilities': {'rest': 1.0},
                                                    'confidence': 1.0, 'confidence_source': 'deterministic'}}


def test_sidecar_legacy_native_answer_remains_usable_without_fabricated_confidence():
    native = SimpleNamespace(ask=lambda *_: {'action': {'choice': 'rest'}})
    result = decide(native, _packet())
    assert result['decisions'] == {'action': 'rest'}
    assert result['decision_details'] == {'action': {'choice': 'rest', 'confidence_source': 'unavailable'}}


@pytest.mark.parametrize('synchronous', [False, True])
def test_detailed_api_preserves_metadata_and_settles_once(monkeypatch, synchronous):
    port, calls, settlements = _port(monkeypatch, {'action': DETAIL})
    result = port.ask_detailed_sync({}, QUESTIONS, purpose='synthetic') if synchronous else asyncio.run(
        port.ask_detailed({}, QUESTIONS, purpose='synthetic'))
    assert result == {'action': DETAIL}
    assert len(calls) == len(settlements) == 1
    assert settlements == [({'receipt': 'synthetic'}, hashlib.sha256(calls[0].data).hexdigest())]


@pytest.mark.parametrize('detailed', [False, True])
def test_legacy_sidecar_response_supports_both_apis(monkeypatch, detailed):
    port, calls, settlements = _port(monkeypatch, None)
    result = asyncio.run((port.ask_detailed if detailed else port.ask)({}, QUESTIONS, purpose='synthetic'))
    assert result == ({'action': {'choice': 'rest', 'confidence_source': 'unavailable'}} if detailed
                      else {'action': 'rest'})
    assert len(calls) == len(settlements) == 1


def test_existing_choice_api_drops_valid_metadata_after_validation(monkeypatch):
    port, calls, settlements = _port(monkeypatch, {'action': DETAIL})
    assert port.ask_sync({}, QUESTIONS, purpose='synthetic') == {'action': 'rest'}
    assert len(calls) == len(settlements) == 1


ROUNDING_BOUNDARY_DETAILS = [
    {'choice': 'rest', 'probabilities': {'rest': 1., 'continue': 0.}, 'confidence': .98},
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .2}, 'confidence': .62},
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .2}, 'confidence': .58},
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .22}, 'confidence': .6},
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .18}, 'confidence': .6},
]


@pytest.mark.parametrize('detail', ROUNDING_BOUNDARY_DETAILS)
def test_native_rounding_at_inclusive_tolerance_preserves_upstream_numbers(detail):
    result = decide(SimpleNamespace(ask=lambda *_: {'action': detail}), _packet())
    expected = {**detail, 'confidence_source': 'upstream'}
    assert result['decision_details'] == {'action': expected}


@pytest.mark.parametrize('detail', ROUNDING_BOUNDARY_DETAILS)
@pytest.mark.parametrize('detailed', [False, True])
def test_transport_rounding_boundary_validates_before_exactly_one_settlement(monkeypatch, detail, detailed):
    port, calls, settlements = _port(monkeypatch, {'action': detail})
    result = (port.ask_detailed_sync if detailed else port.ask_sync)({}, QUESTIONS, purpose='synthetic')
    assert result == ({'action': {**detail, 'confidence_source': 'upstream'}} if detailed else {'action': 'rest'})
    assert len(calls) == len(settlements) == 1
    assert settlements[0][1] == hashlib.sha256(calls[0].data).hexdigest()


@pytest.mark.parametrize('detail', [
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .2201}, 'confidence': .6},
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .1799}, 'confidence': .6},
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .2}, 'confidence': 1.0001},
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .2}, 'confidence': -.0001},
    {'choice': 'other', 'probabilities': {'rest': .8, 'continue': .2}, 'confidence': .6},
])
def test_material_deviation_stays_rejected_by_service_and_before_settlement(monkeypatch, detail):
    with pytest.raises(ValueError, match='invalid_provider_response'):
        decide(SimpleNamespace(ask=lambda *_: {'action': detail}), _packet())
    port, calls, settlements = _port(monkeypatch, {'action': detail})
    with pytest.raises(ValueError, match='JEV_RESPONSE_INVALID'):
        port.ask_detailed_sync({}, QUESTIONS, purpose='synthetic')
    assert len(calls) == 1 and settlements == []


INVALID_DETAILS = [
    {'choice': 'rest', 'probabilities': {'rest': .8}, 'confidence': .6},
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .2, 'other': 0}, 'confidence': .6},
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .1}, 'confidence': .6},
    {'choice': 'rest', 'probabilities': {'rest': float('nan'), 'continue': .2}},
    {'choice': 'rest', 'probabilities': {'rest': float('inf'), 'continue': .2}},
    {'choice': 'rest', 'probabilities': {'rest': 10 ** 400, 'continue': .2}},
    {'choice': 'rest', 'probabilities': {'rest': True, 'continue': 0}},
    {'choice': 'rest', 'probabilities': {'rest': 1.1, 'continue': -.1}},
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .2}, 'confidence': True},
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .2}, 'confidence': 1.1},
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .2}, 'confidence': float('nan')},
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .2}, 'confidence': 10 ** 400},
    {'choice': 'rest', 'confidence': .8},
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .2}, 'confidence_source': 'upstream'},
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .2}, 'confidence_source': 'unrecognized'},
    {'choice': 'rest', 'probabilities': {'rest': 1, 'continue': 0}, 'confidence': 1,
     'confidence_source': 'deterministic'},
]


OBSERVED_NATIVE_METADATA = json.loads((Path(__file__).parent / 'fixtures' /
                                      'jev_native_metadata_observations.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('record', OBSERVED_NATIVE_METADATA, ids=lambda record: record['case'])
def test_saved_native_observation_keeps_actual_choice_and_numbers_in_service(record):
    key, answer = record['question_id'], record['answer']
    questions = {key: {'instructions': 'Replay saved numeric metadata only',
                       'criteria': {option: option for option in record['criteria_keys']}}}
    result = decide(SimpleNamespace(ask=lambda *_: {key: {'type': 'choice', **answer}}), _packet(questions))
    assert result['decisions'] == {key: answer['choice']}
    assert result['decision_details'] == {key: {**answer, 'confidence_source': 'upstream'}}


@pytest.mark.parametrize('record', OBSERVED_NATIVE_METADATA, ids=lambda record: record['case'])
def test_saved_native_observation_survives_detailed_transport_and_one_settlement(monkeypatch, record):
    key, answer = record['question_id'], record['answer']
    questions = {key: {'instructions': 'Replay saved numeric metadata only',
                       'criteria': {option: option for option in record['criteria_keys']}}}
    expected = {key: {**answer, 'confidence_source': 'upstream'}}
    port, calls, settlements = _port(monkeypatch, expected, decisions={key: answer['choice']})
    assert port.ask_detailed_sync({}, questions, purpose='synthetic') == expected
    assert len(calls) == len(settlements) == 1
    assert settlements[0][1] == hashlib.sha256(calls[0].data).hexdigest()


@pytest.mark.parametrize('detail', [
    {'choice': 'rest', 'probabilities': {'rest': .2, 'continue': .8}},
    {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .2}, 'confidence_source': 'unavailable'},
])
def test_distribution_without_native_confidence_can_be_retained_as_unavailable(monkeypatch, detail):
    expected = {**detail, 'confidence_source': 'unavailable'}
    result = decide(SimpleNamespace(ask=lambda *_: {'action': detail}), _packet())
    assert result['decision_details'] == {'action': expected}
    port, calls, settlements = _port(monkeypatch, {'action': expected})
    assert port.ask_detailed_sync({}, QUESTIONS, purpose='synthetic') == {'action': expected}
    assert len(calls) == len(settlements) == 1


@pytest.mark.parametrize('confidence', [.95, .6201, .5799])
def test_bounded_native_confidence_is_observation_without_a_formula_gate(monkeypatch, confidence):
    detail = {'choice': 'rest', 'probabilities': {'rest': .8, 'continue': .2},
              'confidence': confidence, 'confidence_source': 'upstream'}
    result = decide(SimpleNamespace(ask=lambda *_: {'action': detail}), _packet())
    assert result['decision_details'] == {'action': detail}
    port, calls, settlements = _port(monkeypatch, {'action': detail})
    assert port.ask_detailed_sync({}, QUESTIONS, purpose='synthetic') == {'action': detail}
    assert len(calls) == len(settlements) == 1


def test_legal_detail_choice_must_still_match_decisions_before_settlement(monkeypatch):
    detail = {'choice': 'continue', 'probabilities': {'rest': .8, 'continue': .2}, 'confidence': .6}
    port, calls, settlements = _port(monkeypatch, {'action': detail})
    with pytest.raises(ValueError, match='JEV_RESPONSE_INVALID'):
        port.ask_detailed_sync({}, QUESTIONS, purpose='synthetic')
    assert len(calls) == 1 and settlements == []


@pytest.mark.parametrize('detail', INVALID_DETAILS)
@pytest.mark.parametrize('detailed', [False, True])
def test_invalid_metadata_is_rejected_before_any_settlement(monkeypatch, detail, detailed):
    port, calls, settlements = _port(monkeypatch, {'action': detail})
    with pytest.raises(ValueError, match='JEV_RESPONSE_INVALID'):
        (port.ask_detailed_sync if detailed else port.ask_sync)({}, QUESTIONS, purpose='synthetic')
    assert len(calls) == 1 and settlements == []


@pytest.mark.parametrize('detail', INVALID_DETAILS)
def test_sidecar_rejects_invalid_native_metadata(detail):
    native = SimpleNamespace(ask=lambda *_: {'action': detail})
    with pytest.raises(ValueError, match='invalid_provider_response'):
        decide(native, _packet())


@pytest.mark.parametrize('probabilities,expected', [
    ({'rest': .5, 'continue': .5}, 0.0),
    ({'rest': 1.0, 'continue': 0.0}, 1.0),
    ({'rest': .6, 'continue': .3, 'other': .1}, .4),
])
def test_detailed_transport_derives_the_documented_choice_confidence(monkeypatch, probabilities, expected):
    questions = {'action': {'instructions': 'Choose', 'criteria': {key: key for key in probabilities}}}
    port, calls, settlements = _port(monkeypatch, {'action': {'choice': 'rest', 'probabilities': probabilities}})
    detail = port.ask_detailed_sync({}, questions, purpose='synthetic')['action']
    assert detail['probabilities'] == probabilities
    assert detail['confidence'] == pytest.approx(expected)
    assert detail['confidence_source'] == 'probability-derived'
    assert len(calls) == len(settlements) == 1


def test_detailed_transport_accepts_trustworthy_singleton_marker(monkeypatch):
    detail = {'choice': 'rest', 'probabilities': {'rest': 1.0}, 'confidence': 1.0,
              'confidence_source': 'deterministic'}
    questions = {'action': {'instructions': 'Only possible value', 'criteria': {'rest': 'Rest'}}}
    port, calls, settlements = _port(monkeypatch, {'action': detail})
    assert port.ask_detailed_sync({}, questions, purpose='synthetic') == {'action': detail}
    assert len(calls) == len(settlements) == 1


def _dependent_batch():
    questions = {}
    for i in range(12):
        questions[f'update_{i}_existing_match'] = {'instructions': 'Match only this bound action and round',
                                                 'criteria': {'none': 'No existing match',
                                                              'p0': {'id': 'synthetic-project', 'title': 'Synthetic action',
                                                                     'kind': 'linli', 'status': 'planned', 'actor': 'linli'}}}
        questions[f'update_{i}_applicability_kind'] = {'instructions': 'Whether only this bound action applies',
                                                     'criteria': {'none': 'Not applicable', 'linli': 'Character action',
                                                                  'shared': 'Shared action'}}
        questions[f'update_{i}_status'] = {'instructions': 'Status of only this bound action',
                                         'criteria': {key: key for key in ('none', 'planned', 'ongoing', 'paused',
                                                                         'completed', 'cancelled', 'awaiting_user')}}
        questions[f'update_{i}_evidence'] = {'instructions': 'Evidence for only this bound action',
                                           'criteria': {'none': 'No proof',
                                                        'r0': {'source': 'linli_reply', 'text': 'First quote', 'span': [0, 11]},
                                                        'r1': {'source': 'linli_reply', 'text': 'Second quote', 'span': [11, 23]}}}
    for i in range(4):
        questions[f'boundary_{i}_action'] = {'instructions': 'Only this existing boundary',
                                           'criteria': {'none': 'No action', 'new': 'New boundary',
                                                        'set_0': 'Update', 'withdraw_0': 'Withdraw'}}
    questions.update({
        'sleep_hour': {'instructions': 'Hour from only the bound routine quote',
                       'criteria': {'unknown': 'Unknown', **{str(i): str(i) for i in range(24)}}},
        'sleep_minute': {'instructions': 'Whole minute from only the bound routine quote',
                         'criteria': {'unknown': 'Unknown', **{str(i): str(i) for i in range(60)}}},
        'utc_offset': {'instructions': 'Whole UTC offset in minutes from only the bound routine quote',
                       'criteria': {'unknown': 'Unknown', **{str(i): str(i) for i in range(-720, 841, 15)}}},
        'utc_offset_literal': {'instructions': 'Select only an applicable original UTC literal for the bound routine',
                               'criteria': {'none': 'No applicable literal',
                                            't0': {'source': 'user_letter', 'text': 'UTC+09:00', 'span': [0, 9]},
                                            't1': {'source': 'user_letter', 'text': 'UTC-09:00', 'span': [9, 18]}}},
    })
    return questions


def _batch_details(questions, *, non_max_question=None):
    answers = {}
    for key, question in questions.items():
        options = question['criteria']
        routine = {'sleep_hour': '22', 'sleep_minute': '45', 'utc_offset': '540', 'utc_offset_literal': 't0'}
        choice = (routine[key] if key in routine else 'completed' if key.endswith('_status')
                  else 'none' if key.endswith('_existing_match') else 'linli' if key.endswith('_applicability_kind')
                  else 'r0' if key.endswith('_evidence') else 'withdraw_0')
        answers[key] = {'choice': choice, 'probabilities': {
            option: .9 if option == choice else .1 / (len(options) - 1) for option in options}}
        if key == non_max_question:
            maximum = next(option for option in options if option != choice)
            answers[key]['probabilities'] = {
                option: .9 if option == maximum else .1 / (len(options) - 1) for option in options}
    result = decide(SimpleNamespace(ask=lambda *_: answers), _packet(questions))
    return result['decisions'], result['decision_details']


def test_complete_second_phase_batch_preserves_every_question_detail(monkeypatch):
    questions = _dependent_batch()
    choices, details = _batch_details(questions)
    port, calls, settlements = _port(monkeypatch, details, decisions=choices)
    actual = asyncio.run(port.ask_detailed({}, questions, purpose='exchange-anchored-facts'))
    assert len(questions) == 56 and set(actual) == set(questions) and actual == details
    assert set(actual['sleep_minute']['probabilities']) == {'unknown', *map(str, range(60))}
    assert actual['sleep_minute']['choice'] == '45'
    assert set(actual['utc_offset']['probabilities']) == {'unknown', *map(str, range(-720, 841, 15))}
    assert actual['utc_offset']['choice'] == '540'
    assert set(actual['utc_offset_literal']['probabilities']) == {'none', 't0', 't1'}
    assert actual['utc_offset_literal']['choice'] == 't0'
    assert all(item['confidence_source'] == 'probability-derived' for item in actual.values())
    assert len(calls) == len(settlements) == 1
    assert json.loads(calls[0].data)['purpose'] == 'exchange-anchored-facts'


def test_complete_batch_keeps_non_max_native_choice_without_inventing_confidence(monkeypatch):
    questions = _dependent_batch()
    key = 'update_11_evidence'
    choices, details = _batch_details(questions, non_max_question=key)
    assert details[key]['choice'] == choices[key] == 'r0'
    assert 'confidence' not in details[key] and details[key]['confidence_source'] == 'unavailable'
    port, calls, settlements = _port(monkeypatch, details, decisions=choices)
    actual = port.ask_detailed_sync({}, questions, purpose='exchange-anchored-facts')
    assert len(actual) == 56 and actual == details
    assert len(calls) == len(settlements) == 1


@pytest.mark.parametrize('question_id', ['update_0_existing_match', 'update_0_applicability_kind',
                                        'update_6_status', 'update_11_evidence', 'boundary_0_action',
                                        'sleep_minute', 'utc_offset', 'utc_offset_literal'])
def test_each_second_phase_detail_is_validated_before_settlement(monkeypatch, question_id):
    questions = _dependent_batch()
    choices, details = _batch_details(questions)
    details[question_id]['confidence'] = True
    port, calls, settlements = _port(monkeypatch, details, decisions=choices)
    with pytest.raises(ValueError, match='JEV_RESPONSE_INVALID'):
        port.ask_detailed_sync({}, questions, purpose='exchange-anchored-facts')
    assert len(calls) == 1 and settlements == []


def test_sidecar_supports_self_contained_identity_options_without_changing_their_keys():
    project = {'id': 'synthetic-project', 'title': 'Synthetic action', 'kind': 'linli',
               'status': 'planned', 'actor': 'linli'}
    questions = {'update_0_existing_match': {'instructions': 'Match this action and round',
                                           'criteria': {'none': 'No existing match', 'p0': project}}}

    def native(_state, received):
        assert received['update_0_existing_match']['criteria']['p0'] == project
        return {'update_0_existing_match': {'choice': 'p0', 'probabilities': {'none': .1, 'p0': .9}}}

    result = decide(SimpleNamespace(ask=native), _packet(questions))
    assert result['decisions'] == {'update_0_existing_match': 'p0'}
    assert result['decision_details']['update_0_existing_match']['choice'] == 'p0'


@pytest.mark.parametrize('answers', [{}, {'other': {'choice': 'rest'}}, [], {'action': 'rest'}])
def test_sidecar_rejects_missing_or_unexpected_native_answers(answers):
    with pytest.raises(ValueError, match='invalid_provider_response'):
        decide(SimpleNamespace(ask=lambda *_: answers), _packet())


@pytest.mark.parametrize('details', [[], {'unknown': DETAIL}, {'action': []}])
def test_invalid_details_structure_never_settles(monkeypatch, details):
    port, calls, settlements = _port(monkeypatch, details)
    with pytest.raises(ValueError, match='JEV_RESPONSE_INVALID'):
        port.ask_sync({}, QUESTIONS, purpose='synthetic')
    assert len(calls) == 1 and settlements == []


@pytest.mark.parametrize('method', ['ask', 'ask_sync', 'ask_detailed', 'ask_detailed_sync'])
def test_empty_questions_never_request_or_settle(monkeypatch, method):
    port, calls, settlements = _port(monkeypatch, None)
    result = getattr(port, method)({}, {}, purpose='synthetic')
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    assert result == {} and calls == settlements == []

import asyncio
import pytest

from runtime.reply.companion_decision import _json
from runtime.reply.jev_quality import _ask, _question_batches
from runtime.reply.jev_questions import SEMANTIC_REQUEST_MAX_BYTES


def questions(count):
    return {f'q{i}': {'instructions': '完整判断' * 30, 'criteria': {'yes': '是', 'no': '否'}} for i in range(count)}


def test_utf8_batches_preserve_complete_state_and_all_questions():
    state = {'frozen_world': '课程与来源' * 1600, 'recent_dialogue': ['原始记录']}
    calls = []
    class Port:
        async def ask(self, actual, batch, *, purpose):
            assert actual == state
            assert len(_json({'state': actual, 'questions': batch, 'purpose': purpose}).encode()) <= SEMANTIC_REQUEST_MAX_BYTES
            calls.append(batch)
            return {key: 'no' for key in batch}
    source = questions(48)
    result = asyncio.run(_ask(Port(), state, source, 'quality_continuity_memory'))
    assert len(calls) > 1
    assert result == {key: 'no' for key in source}
    assert [key for batch in calls for key in batch] == list(source)


def test_oversized_single_question_fails_before_any_partial_calls():
    calls = []
    class Port:
        async def ask(self, *args, **kwargs):
            calls.append(1)
    source = questions(1)
    source['too_big'] = {'instructions': '中' * 11000, 'criteria': {'yes': '是'}}
    with pytest.raises(ValueError, match='JEV_INPUT_TOO_LARGE'):
        asyncio.run(_ask(Port(), {'complete': 'unchanged'}, source, 'quality_test'))
    assert calls == []


def test_oversized_evidence_is_not_silently_trimmed():
    with pytest.raises(ValueError, match='JEV_INPUT_TOO_LARGE'):
        _question_batches({'evidence': '中' * 11000}, questions(1), 'quality_test')


def test_question_count_limit_still_applies_with_tiny_payload():
    batches = _question_batches({}, {str(i): {'criteria': {'yes': 'Y'}} for i in range(100)}, 'quality_test')
    assert [len(batch) for batch in batches] == [48, 48, 4]


def test_near_limit_full_evidence_splits_even_only_five_questions():
    state = {'full_recent_dialogue_and_authority': 'x' * 31000}
    source = questions(5)
    full = _json({'state': state, 'questions': source, 'purpose': 'quality_autonomy_life'}).encode()
    assert len(full) > 32768
    batches = _question_batches(state, source, 'quality_autonomy_life')
    assert len(batches) > 1
    assert [key for batch in batches for key in batch] == list(source)
    assert all(len(_json({'state': state, 'questions': batch, 'purpose': 'quality_autonomy_life'}).encode()) <= 32768 for batch in batches)

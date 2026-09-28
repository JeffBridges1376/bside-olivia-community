"""Outer deadlines cover optional semantic execution, never alter model budgets."""
from types import SimpleNamespace

import pytest

from runtime.memory.recall_check import RECALL_CHECK_TIMEOUT_SECONDS
from runtime.reply.reply_reviewer import NullReviewer


@pytest.fixture
def budget(monkeypatch):
    import local_server as server
    monkeypatch.setattr(server, 'LLM_TIMEOUT_SECONDS', 30)
    monkeypatch.setattr(server, 'supports_scoped_reasoning', lambda _: False)
    monkeypatch.setattr(server, 'resolve_model_quality_config', lambda *a, **k:
        SimpleNamespace(timeout_seconds=10, reasoning_timeout_seconds=50))
    monkeypatch.setattr(server, 'current_turn_interpretation_enabled', lambda: False)
    monkeypatch.setattr(server, 'reply_pipeline', SimpleNamespace(
        reviewer=NullReviewer(), rewriter=SimpleNamespace(), current_turn_interpreter=None))
    return server


def test_qq_environment_interpreter_uses_reasoning_deadline(budget, monkeypatch):
    monkeypatch.setattr(budget, 'current_turn_interpretation_enabled', lambda: True)
    assert budget._reply_pipeline_timeout_seconds('future_im') == RECALL_CHECK_TIMEOUT_SECONDS + 30 + 30 + 50 + 5


@pytest.mark.parametrize('mode', ['future_im', 'text_letter'])
def test_explicit_interpreter_uses_its_own_timeout_without_environment_flag(budget, mode):
    budget.reply_pipeline.current_turn_interpreter = SimpleNamespace(timeout_seconds=83)
    reserve = 350 if mode == 'text_letter' else 30
    assert budget._reply_pipeline_timeout_seconds(mode) == RECALL_CHECK_TIMEOUT_SECONDS + 30 + reserve + 83 + 5


@pytest.mark.parametrize('mode,reasoning,reserve', [
    ('future_im', 50, 50),  # two parallel attempts + rewrite + two parallel attempts
    ('text_letter', None, 70),  # each review adds a separate evidence adjudication
    ('text_letter', 50, 750),  # five layers, two slots, retries, adjudication, two reviews
])
def test_enabled_review_covers_retries_and_rewrite(budget, monkeypatch, mode, reasoning, reserve):
    monkeypatch.setattr(budget, 'resolve_model_quality_config', lambda *a, **k:
        SimpleNamespace(timeout_seconds=10, reasoning_timeout_seconds=reasoning))
    budget.reply_pipeline.reviewer = object()
    assert budget._reply_pipeline_timeout_seconds(mode) == RECALL_CHECK_TIMEOUT_SECONDS + 30 + reserve + 5


def test_explicit_review_and_rewriter_timeouts_are_counted_separately(budget):
    budget.reply_pipeline.reviewer = SimpleNamespace(adapter=SimpleNamespace(
        config=SimpleNamespace(timeout_seconds=21)), _transport=SimpleNamespace(reasoning_timeout_seconds=None))
    budget.reply_pipeline.rewriter = SimpleNamespace(timeout_seconds=37, reasoning_timeout_seconds=None)
    assert budget._reply_pipeline_timeout_seconds('future_im') == RECALL_CHECK_TIMEOUT_SECONDS + 30 + 4 * 21 + 37 + 5


def test_inactive_video_does_not_gain_interpreter_or_review_stages(budget, monkeypatch):
    monkeypatch.setattr(budget, 'current_turn_interpretation_enabled', lambda: True)
    budget.reply_pipeline.reviewer = object()
    budget.reply_pipeline.current_turn_interpreter = SimpleNamespace(timeout_seconds=83)
    assert budget._reply_pipeline_timeout_seconds('spoken_video') == RECALL_CHECK_TIMEOUT_SECONDS + 30 + 30 + 5


def test_disabled_ports_keep_existing_reserves(budget):
    assert budget._reply_pipeline_timeout_seconds('future_im') == RECALL_CHECK_TIMEOUT_SECONDS + 65
    assert budget._reply_pipeline_timeout_seconds('text_letter') == RECALL_CHECK_TIMEOUT_SECONDS + 385

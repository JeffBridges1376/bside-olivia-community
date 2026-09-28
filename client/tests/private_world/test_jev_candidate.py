import asyncio
from types import SimpleNamespace

import pytest

from private_world_candidate import GatewayPrivateWorldCandidateAnalyzer, PrivateWorldCandidateAnalysisError
from runtime.reply import jev_questions


class Forbidden:
    async def complete(self, *args, **kwargs):
        raise AssertionError('text semantic fallback forbidden')


def test_jev_candidate_is_review_only_and_does_not_call_text_model(monkeypatch):
    class Port:
        async def ask(self, state, questions, **kwargs):
            assert kwargs['purpose'] == 'private_world_candidate'
            assert state['canonical_excerpt'] == 'thanks'
            return {'candidate': 'boundary_respected', 'confidence': 'high'}
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: Port())
    analyzer = GatewayPrivateWorldCandidateAnalyzer(Forbidden(), timeout_seconds=1)
    result = asyncio.run(analyzer.analyze(SimpleNamespace(to_dict=lambda: {'canonical_excerpt': 'thanks'})))
    assert result.candidate_type.value == 'boundary_respected'
    assert result.confidence == 0.9 and result.summary.startswith('待人工审核')


def test_jev_candidate_failure_has_no_gateway_fallback(monkeypatch):
    class Port:
        async def ask(self, *args, **kwargs):
            raise ValueError('JEV_UNAVAILABLE')
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: Port())
    analyzer = GatewayPrivateWorldCandidateAnalyzer(Forbidden(), timeout_seconds=1)
    with pytest.raises(PrivateWorldCandidateAnalysisError, match='ANALYSIS_UNAVAILABLE'):
        asyncio.run(analyzer.analyze(SimpleNamespace(to_dict=lambda: {})))


def test_candidate_runtime_can_use_jev_without_text_gateway(tmp_path):
    from private_world_candidate import create_private_world_candidate_runtime
    from private_world_ledger import SQLitePrivateWorldLedger
    SQLitePrivateWorldLedger(tmp_path / 'world.db')
    runtime = create_private_world_candidate_runtime(Forbidden(), database_path=tmp_path / 'world.db',
        gateway_ready=False, environ={'OLIVIA_PRIVATE_WORLD_CANDIDATES_ENABLED': 'true',
                                     'OLIVIA_JEV_DECISION_URL': 'http://127.0.0.1:8097/v1/companion/decide'})
    assert runtime.provider == 'jev' and runtime.status == 'available'

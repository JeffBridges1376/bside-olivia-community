import asyncio
import json

import pytest

from llm_gateway import GatewayConfig, GatewayRequestScope, OpenAICompatibleAdapter
from original_client_relay_api import RELAY_BASE


@pytest.fixture(autouse=True)
def verified_relay(monkeypatch):
    monkeypatch.setenv('OLIVIA_JEV_FORCED_SEARCH_VERIFIED', '1')


class Choices:
    def __init__(self, decision='q0', fail=False):
        self.decision, self.fail, self.calls = decision, fail, []

    async def ask(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        if self.fail:
            raise ValueError('JEV_UNAVAILABLE')
        from runtime.reply.jev_semantic_service import decide
        from types import SimpleNamespace
        native = SimpleNamespace(ask=lambda state, questions: {'query': {'choice': self.decision}})
        return decide(native, {'state': state, 'questions': questions, 'purpose': purpose})['decisions']


@pytest.mark.parametrize('decision,fail', [('q0', False), ('none', False), ('q0', True)])
def test_jev_search_is_separate_from_private_reply_and_never_model_decided(monkeypatch, decision, fail):
    decisions = Choices(decision, fail)
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: decisions)
    gateway = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible', model='qwen3.7-flash', base_url=RELAY_BASE))
    calls = []
    async def post(body, request, **kwargs):
        calls.append(body)
        text = '公开天气摘要' if body.get('enable_search') else '回复正文'
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}],
                'search_usage': {'count': 1, 'source_count': 1}}
    monkeypatch.setattr(gateway, '_post_json', post)
    messages = [{'role': 'system', 'content': '私人历史和秘密，不得发往搜索'},
                {'role': 'user', 'content': '上海明天天气怎么样？'}]
    result = asyncio.run(gateway.complete_scoped(messages, scope=GatewayRequestScope.TEXT_LETTER_MAX_REASONING))
    assert result.text == '回复正文'
    assert len(decisions.calls) == 1
    assert calls[-1]['enable_search'] is False
    assert '自行判断是否搜索' not in json.dumps(calls[-1], ensure_ascii=False)
    if decision != 'none' and not fail:
        assert len(calls) == 2 and calls[0]['enable_search'] is True
        assert calls[0]['search_options']['forced_search'] is True
        assert '私人历史和秘密' not in json.dumps(calls[0], ensure_ascii=False)
        assert '公开天气摘要' in json.dumps(calls[-1], ensure_ascii=False)
    else:
        assert len(calls) == 1


def test_structured_repair_reuses_search_result(monkeypatch):
    decisions = Choices()
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: decisions)
    gateway = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible', model='qwen3.7-flash', base_url=RELAY_BASE))
    calls = []
    async def post(body, request, **kwargs):
        calls.append(body)
        text = '公开天气摘要' if body.get('enable_search') else ('invalid' if len(calls) == 2 else '{"reply":"晴"}')
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}],
                'search_usage': {'count': 1, 'source_count': 1}}
    monkeypatch.setattr(gateway, '_post_json', post)
    asyncio.run(gateway.complete_structured_scoped([{'role': 'user', 'content': '上海明天天气怎么样？'}],
        scope=GatewayRequestScope.PERSONAL_CHAT_JSON,
        response_format={'type': 'json_schema', 'schema': {'type': 'object', 'required': ['reply'], 'properties': {'reply': {'type': 'string'}}}}))
    assert len(decisions.calls) == 1
    assert sum(body.get('enable_search') is True for body in calls) == 1


@pytest.mark.parametrize('verified', [False, True])
def test_unverified_relay_or_missing_execution_evidence_never_claims_search(monkeypatch, verified):
    if not verified:
        monkeypatch.delenv('OLIVIA_JEV_FORCED_SEARCH_VERIFIED')
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: Choices())
    gateway = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible', model='qwen3.7-flash', base_url=RELAY_BASE))
    calls = []
    async def post(body, request, **kwargs):
        calls.append(body)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'unverified external answer'}}]}
    monkeypatch.setattr(gateway, '_post_json', post)
    asyncio.run(gateway.complete_scoped([{'role': 'user', 'content': '上海明天天气怎么样？'}],
        scope=GatewayRequestScope.TEXT_LETTER_MAX_REASONING))
    assert len(calls) == (2 if verified else 1)
    assert calls[-1]['enable_search'] is False
    assert 'unverified external answer' not in json.dumps(calls[-1], ensure_ascii=False)
    assert '查询未完成' in json.dumps(calls[-1], ensure_ascii=False)


@pytest.mark.parametrize('endpoint', ['http://example.org/v1', 'http://localhost:80/v1',
    'http://127.0.0.1:80/v1?x=1', 'http://user@127.0.0.1:80/v1', 'https://127.0.0.1:80/v1'])
def test_dev_search_rejects_nonliteral_loopback_endpoint(monkeypatch, endpoint):
    from runtime.reply.web_search import _development_relay
    monkeypatch.setenv('OLIVIA_JEV_SEARCH_DEV_BASE_URL', endpoint)
    monkeypatch.setenv('OLIVIA_JEV_SEARCH_DEV_TOKEN', 'synthetic')
    gateway = OpenAICompatibleAdapter(GatewayConfig())
    with pytest.raises(ValueError, match='SEARCH_DEV_ENDPOINT_INVALID'):
        _development_relay(gateway)


@pytest.mark.parametrize('token', ['', 'synthetic-main'])
def test_dev_search_never_reuses_main_provider_credential(monkeypatch, token):
    from runtime.reply.web_search import _development_relay
    monkeypatch.setenv('OLIVIA_JEV_SEARCH_DEV_BASE_URL', 'http://127.0.0.1:10000/v1')
    monkeypatch.setenv('OLIVIA_JEV_SEARCH_DEV_TOKEN', token)
    monkeypatch.setenv('SYNTHETIC_MAIN_KEY', 'synthetic-main')
    gateway = OpenAICompatibleAdapter(GatewayConfig(api_key_env='SYNTHETIC_MAIN_KEY'))
    with pytest.raises(ValueError, match='SEARCH_DEV_REQUIRES_DISTINCT_TOKEN'):
        _development_relay(gateway)

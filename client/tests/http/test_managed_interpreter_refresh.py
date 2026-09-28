"""Settings replacement must rebuild optional interpretation on the new credential."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from llm_gateway import GatewayConfig, ManagedLLMConfig
from runtime.reply import reply_pipeline as pipeline_module


@pytest.fixture
def runtime(monkeypatch):
    import local_server as server

    old_gateway = object()
    adapter = SimpleNamespace(config=GatewayConfig(provider='mock'), gateway=old_gateway,
                              persona_v2_path='unused-synthetic-persona')
    def replace_runtime(config, gateway):
        adapter.config, adapter.gateway = config, gateway
    adapter.replace_runtime = replace_runtime
    for name, value in {
        'LLM_CONFIG': adapter.config, 'LLM_TIMEOUT_SECONDS': 1, 'LLM_CFG': {},
        'letters_adapter': adapter, 'reply_pipeline': object(),
        'reply_engine': SimpleNamespace(gateway=SimpleNamespace(adapter=adapter), timeout_seconds=1),
        'emotion_triage': SimpleNamespace(gateway=old_gateway),
        'private_world_candidate_analyzer': None, 'conversation_memory_adapter': object(),
    }.items():
        monkeypatch.setattr(server, name, value)
    monkeypatch.setattr(server, 'create_conversation_memory_adapter', lambda *a, **kw: object())
    monkeypatch.setattr(server, 'create_model_quality_ports', lambda *a, **kw: (None, None))
    monkeypatch.delenv('OLIVIA_LLM_RUNTIME_KEY_CONFIGURED', raising=False)
    # Runtime discovery would inspect the old main adapter while replacing it.
    def unexpected_discovery(*a, **kw):
        raise AssertionError('Do not discover stale runtime quality ports')
    monkeypatch.setattr(pipeline_module, 'create_model_quality_ports', unexpected_discovery)
    calls = []
    def create_gateway(config, *, key_resolver):
        async def complete_structured_scoped(messages, **kwargs):
            text = messages[-1]['content']
            calls.append((config.base_url, config.model, key_resolver(), text))
            return SimpleNamespace(text=json.dumps({'acts': [
                {'quote': text, 'kind': 'self_statement', 'meaning': text}]}))
        return SimpleNamespace(config=config, complete_structured_scoped=complete_structured_scoped)
    monkeypatch.setattr(server, 'create_gateway', create_gateway)
    return server, calls


@pytest.mark.parametrize('enabled', [None, False, True])
def test_settings_refresh_interpreter_uses_each_new_gateway_and_key(runtime, monkeypatch, enabled):
    server, calls = runtime
    if enabled is None:
        monkeypatch.delenv('OLIVIA_LETTER_CURRENT_TURN_INTERPRETATION', raising=False)
    else:
        monkeypatch.setenv('OLIVIA_LETTER_CURRENT_TURN_INTERPRETATION', str(enabled))
    monkeypatch.setenv('OLIVIA_REPLY_REVIEW_TIMEOUT_SECONDS', '77')
    interpreters = []
    for version in ('first', 'second'):
        server.apply_runtime_llm_config(ManagedLLMConfig(
            provider='openai_compatible', base_url=f'https://{version}.example/v1',
            model=f'{version}-model', max_retries=0), f'synthetic-{version}-key')
        interpreter = server.reply_pipeline.current_turn_interpreter
        if not enabled:
            assert interpreter is None
            continue
        assert interpreter is not None
        assert interpreter.gateway is server.letters_adapter.gateway
        assert interpreter.timeout_seconds == 77.0
        asyncio.run(interpreter.interpret('我醒了'))
        interpreters.append(interpreter)
    if enabled:
        assert interpreters[0] is not interpreters[1]
        assert calls == [(f'https://{v}.example/v1', f'{v}-model', f'synthetic-{v}-key', '我醒了')
                         for v in ('first', 'second')]
    else:
        assert not calls


def test_interpreter_construction_failure_preserves_previous_runtime(runtime, monkeypatch):
    server, calls = runtime
    monkeypatch.setenv('OLIVIA_LETTER_CURRENT_TURN_INTERPRETATION', 'true')
    previous = (server.reply_pipeline, server.letters_adapter.gateway, server.LLM_CONFIG)
    def fail(*a, **kw):
        raise RuntimeError('synthetic interpreter construction failure')
    monkeypatch.setattr(pipeline_module, 'CurrentTurnInterpreter', fail)
    with pytest.raises(RuntimeError, match='synthetic interpreter'):
        server.apply_runtime_llm_config(ManagedLLMConfig(
            provider='openai_compatible', base_url='https://new.example/v1',
            model='new-model', max_retries=0), 'synthetic-new-key')
    assert (server.reply_pipeline, server.letters_adapter.gateway, server.LLM_CONFIG) == previous
    assert not calls

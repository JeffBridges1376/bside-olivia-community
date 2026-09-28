import json
from pathlib import Path
from types import SimpleNamespace

from runtime.reply import reply_model_quality as quality
from runtime.persona.persona_loader import load_persona
from tests.persona.test_reply_model_quality import SequencedQualityGateway
from llm_gateway import GatewayConfig


ROOT = Path(__file__).resolve().parents[2]


def world_messages():
    world = {'kind': 'character_life_reference', 'as_of': '2026-09-28T05:59:00+00:00',
             'current': None, 'schedule': {'date': '2026-09-28', 'current_class': None,
             'classes': [{'title': '钢琴专业课', 'start': '2026-09-28T14:00:00+08:00'}]}}
    wrapper = {'fragment_id': 'linli.daily-life', 'text': json.dumps(world, ensure_ascii=False)}
    return world, ({'role': 'system', 'content': '<evidence_summary>' + json.dumps(wrapper, ensure_ascii=False) + '</evidence_summary>'},)


def test_review_references_preserve_same_frozen_world():
    world, messages = world_messages()
    reviewer = quality.GatewayPersonaReviewer.__new__(quality.GatewayPersonaReviewer)
    captured = {}
    def review(candidate, context, *, references):
        captured['references'] = [r.to_dict() for r in references]
    reviewer.adapter = SimpleNamespace(review=review)
    reviewer.review_with_messages('今天没课。', None, messages)
    assert json.loads(quality._reference_text(captured, 'current.frozen_world')) == world
    assert quality._assembled_world_evidence(({'role': 'user', 'content': messages[0]['content']},)) == ''


def test_review_layers_and_adjudication_receive_frozen_schedule_as_plan():
    world, messages = world_messages()
    evidence = {'assembled_memory': quality._assembled_memory_evidence(messages),
                'frozen_world': quality._assembled_world_evidence(messages)}
    authorities = quality._build_release_layer_authorities(load_persona(ROOT/'linli_character/persona_release_v2.json').snapshot, mode='text_letter')
    for layer in authorities:
        if layer.name not in {'continuity_memory', 'identity_boundary'}:
            continue
        payload = json.loads(quality._layer_messages(layer, candidate='今天没课。', current_user_input='要上课了吧？',
            character_reply_history='', memory_evidence=evidence, relationship_context={}, mode='text_letter', evidence_bound=True)[-1]['content'])
        assert json.loads(payload['frozen_world']) == world
        if layer.name == 'continuity_memory':
            assert 'frozen_world' not in payload['memory_evidence']
            assert payload['memory_evidence']['frozen_world_ref'] == 'frozen_world'
            assert '2026-09-28T14:00:00' not in payload['memory_evidence']['assembled_memory']
        if layer.name == 'continuity_memory':
            schedule = next(s for s in payload['fact_sources'] if s['id'] == 'current_class_schedule')
            assert schedule['kind'] == 'plan'
            assert json.loads(schedule['text']) == world['schedule']
        adjudication = quality._adjudication_support_context('identity_world' if layer.name == 'identity_boundary' else 'continuity_fact',
            authority=layer, current_user_input='', character_reply_history='', memory_evidence=evidence, relationship_context={})
        frozen = adjudication.get('frozen_world') or adjudication['memory_evidence']['frozen_world']
        assert json.loads(frozen) == world


def test_configured_jev_enables_review_by_default_but_respects_explicit_false(monkeypatch):
    monkeypatch.delenv('OLIVIA_REPLY_REVIEW_ENABLED', raising=False)
    monkeypatch.setenv('OLIVIA_JEV_DECISION_URL', 'http://127.0.0.1:8097/v1/companion/decide')
    gateway = SequencedQualityGateway(candidate='候选。', reviews=[])
    adapter = SimpleNamespace(config=GatewayConfig(provider='openai_compatible', base_url='https://example.invalid/v1',
        model='synthetic', api_key_env='SYNTHETIC_KEY'), gateway=gateway,
        persona_v2_path=ROOT/'linli_character/persona_release_v2.json')
    orchestrator = SimpleNamespace(gateway=SimpleNamespace(adapter=adapter))
    assert all(quality.create_model_quality_ports(orchestrator))
    monkeypatch.setenv('OLIVIA_REPLY_REVIEW_ENABLED', 'false')
    assert quality.create_model_quality_ports(orchestrator) == (None, None)


def test_world_deduplication_only_replaces_identical_frozen_record():
    world, messages = world_messages()
    memory = quality._assembled_memory_evidence(messages)
    altered = {**world, 'as_of': 'DIFFERENT_AS_OF'}
    result = quality._world_evidence_references({'assembled_memory': memory,
                                                'frozen_world': json.dumps(altered)})
    assert result['assembled_memory'] == memory


def test_jev_wire_compilation_preserves_authorities_and_world_references():
    from runtime.reply.jev_quality import _review_state
    world, messages = world_messages()
    evidence = {'assembled_memory': quality._assembled_memory_evidence(messages),
                'frozen_world': quality._assembled_world_evidence(messages)}
    layers = quality._build_release_layer_authorities(load_persona(ROOT/'linli_character/persona_release_v2.json').snapshot, mode='text_letter')
    layer = next(x for x in layers if x.name == 'continuity_memory')
    wire = quality._layer_messages(layer, candidate='今天没课。', current_user_input='上课吗？',
        character_reply_history='', memory_evidence=evidence, relationship_context={}, mode='text_letter', evidence_bound=True)
    state = _review_state(layer, wire, {'s0': {'text': '今天没课。', 'start': 0, 'end': 5}})
    contract = state['review_contract']
    assert contract['global'] == layer.global_authority
    assert contract['runtime'] == layer.runtime_authority
    assert set(layer.layer_authority.splitlines()) <= set(contract['global'].splitlines()) | set(contract['layer_specific'].splitlines())
    assert state['input']['frozen_world'] == world
    schedule = next(f for f in state['input']['fact_sources'] if f['id'] == 'current_class_schedule')
    assert schedule['kind'] == 'plan'
    value = state['input'][schedule['source_ref']['root']]
    for key in schedule['source_ref']['path']:
        value = value[key]
    assert value == world['schedule']
    assert 'text' not in schedule
    assert 'candidate_paragraphs' not in state['input']
    assert state['input']['candidate_reply'] == '今天没课。'

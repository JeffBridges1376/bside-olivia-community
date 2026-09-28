"""Core authority and contextual selection use the same frozen declaration set."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from persona_loader import PersonaDeclaration, load_persona
from persona_assembly import assemble_persona
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.prompt_budget import PromptBudgetExceeded


ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / 'linli_character/persona_release_v2.json'
NOW = datetime(2026, 9, 27, 2, tzinfo=timezone.utc)
CONTEXT = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=TrustedTime(NOW))


def snapshot():
    def declaration(key, statement, inclusion, facet='BACKGROUND', **kwargs):
        return PersonaDeclaration(key, 'synthetic.release', 'COMMUNITY_SOFT_CANON',
            'HIGH', 'SUMMARY_ONLY', True, statement, None, facet,
            inclusion=inclusion, **kwargs)
    return replace(load_persona(RELEASE).snapshot, declarations=(
        declaration('trait.core', '她有自己的主见和边界。', 'core', 'CORE_TRAIT'),
        declaration('taste.reading', '她喜欢读有关时间的书。', 'contextual'),
        declaration('taste.music', '她喜欢黑胶唱片。', 'contextual'),
        declaration('phase.piece', '这学期她在练一首指定曲子。', 'phase',
            phase_seed={'title':'合成练习', 'detail':'待进行的合成练习安排。',
                        'activity_kind':'practice', 'scope':'activation_semester'}),
    ))


def core_messages(value=None):
    return assemble_persona(value or snapshot(), CONTEXT, user_input='你平时喜欢读什么？',
        max_units=20000, selected_declaration_ids=()).to_messages()


def test_release_explicitly_classifies_every_declaration_and_keeps_phase_original():
    loaded = load_persona(RELEASE)
    assert loaded.ready
    payload = json.loads(RELEASE.read_text(encoding='utf-8'))
    assert len(payload['declarations']) == 100
    assert all(row.get('inclusion') in {'core', 'contextual', 'phase'} for row in payload['declarations'])
    piece = next(item for item in loaded.snapshot.declarations if item.declaration_id == 'anchor.current_piece')
    assert piece.inclusion == 'phase' and '老师最近' in piece.statement
    assert piece.phase_seed == {
        'title':'肖邦《夜曲 Op.9 No.2》练习',
        'detail':'初始练习安排：持续练习肖邦《夜曲 Op.9 No.2》，关注音色；实际进展由后续生活记录更新。',
        'activity_kind':'practice', 'scope':'activation_semester',
    }
    assert '核心学习方向' not in loaded.snapshot.profile.summary


def test_core_is_required_even_when_original_source_tier_is_soft():
    value = snapshot()
    full = assemble_persona(value, CONTEXT, user_input='你好', max_units=20000)
    limited = assemble_persona(value, CONTEXT, user_input='你好',
        max_units=full.budget_report.required_units)
    assert '她有自己的主见和边界。' in limited.system_content
    assert '她喜欢读有关时间的书。' not in limited.system_content
    assert '这学期她在练一首指定曲子。' not in full.system_content
    with pytest.raises(PromptBudgetExceeded):
        assemble_persona(value, CONTEXT, user_input='你好',
            max_units=full.budget_report.required_units - 1)


def test_selected_ids_cannot_remove_core_or_choose_phase_or_invent_statement():
    value = snapshot()
    result = assemble_persona(value, CONTEXT, user_input='读什么？', max_units=20000,
        selected_declaration_ids=('taste.reading',))
    assert '她有自己的主见和边界。' in result.system_content
    assert '她喜欢读有关时间的书。' in result.system_content
    assert '她喜欢黑胶唱片。' not in result.system_content
    for invalid in [('phase.piece',), ('unknown.id',), ('trait.core',), ('taste.reading', 'taste.reading')]:
        with pytest.raises(ValueError):
            assemble_persona(value, CONTEXT, user_input='读什么？', max_units=20000,
                selected_declaration_ids=invalid)


@pytest.mark.parametrize('mutation', ['core_seed', 'extra_key', 'invalid_kind', 'too_long'])
def test_phase_seed_is_validated_at_loader_boundary(tmp_path, mutation):
    payload = json.loads(RELEASE.read_text(encoding='utf-8'))
    piece = next(row for row in payload['declarations'] if row['declaration_id'] == 'anchor.current_piece')
    if mutation == 'core_seed':
        piece['inclusion'] = 'core'
    elif mutation == 'extra_key':
        piece['phase_seed']['inferred_teacher_feedback'] = 'not allowed'
    elif mutation == 'invalid_kind':
        piece['phase_seed']['activity_kind'] = 'completed'
    else:
        piece['phase_seed']['detail'] = 'x' * 241
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    assert load_persona(path).error_code.value == 'PERSONA_SCHEMA_INVALID'


@pytest.mark.parametrize('result', ['valid', 'invalid', 'phase', 'failure', 'none'])
def test_no_history_uses_one_real_selector_and_failure_degrades_to_core(tmp_path, result):
    from runtime.memory.history_selection import select_history_messages
    calls = []
    async def complete(messages, **kwargs):
        calls.append((messages, kwargs))
        packet = json.loads(messages[-1]['content'])
        assert {item['id'] for item in packet['persona_candidates']} == {'taste.reading', 'taste.music'}
        assert 'phase.piece' not in str(packet) and 'trait.core' not in str(packet['persona_candidates'])
        if result == 'failure':
            raise RuntimeError('synthetic unavailable')
        selected = {'valid':['taste.reading'], 'invalid':['forged'], 'phase':['phase.piece'], 'none':[]}[result]
        return SimpleNamespace(text=json.dumps({'selected_ids':[], 'dependencies':[], 'persona_ids':selected}))
    output = asyncio.run(select_history_messages(core_messages(),
        SimpleNamespace(complete_structured_scoped=complete), max_input_chars=20000,
        persona_snapshot=snapshot(), persona_mode='text_letter'))
    wire = '\n'.join(message['content'] for message in output)
    assert len(calls) == 1
    assert '她有自己的主见和边界。' in wire
    assert ('她喜欢读有关时间的书。' in wire) == (result == 'valid')
    assert '她喜欢黑胶唱片。' not in wire and '这学期她在练一首指定曲子。' not in wire
    if result in {'invalid','phase','failure'}:
        assert '仅保留核心人格' in wire and '保持未知' in wire
    assert sum(len(message['content']) for message in output) <= 20000


def test_selected_optional_projection_cannot_overflow_or_drop_core():
    from runtime.memory.history_selection import select_history_messages
    value = snapshot()
    messages = core_messages(value)
    async def complete(*args, **kwargs):
        return SimpleNamespace(text=json.dumps({'selected_ids':[], 'dependencies':[],
            'persona_ids':['taste.reading','taste.music']}))
    budget = sum(len(message['content']) for message in messages)
    output = asyncio.run(select_history_messages(messages, SimpleNamespace(complete_structured_scoped=complete),
        max_input_chars=budget, persona_snapshot=value, persona_mode='text_letter'))
    assert sum(len(message['content']) for message in output) <= budget
    assert '她有自己的主见和边界。' in str(output)


def test_development_dimensions_belong_to_contextual_baselines_not_immutable_core():
    loaded = load_persona(RELEASE)
    taste = next(item for item in loaded.snapshot.declarations if item.declaration_id == 'anchor.everyday_taste')
    assert taste.inclusion == 'contextual'
    assert {item['key'] for item in taste.development} == {'sweet', 'spicy', 'light_food'}
    assert next(item for item in taste.development if item['key'] == 'spicy')['baseline'] == 'avoid'
    assert all(item['anchor'] is True for item in taste.development)
    for key in ('inferred.familiar_routines', 'anchor.stopping_ritual'):
        assert next(item for item in loaded.snapshot.declarations if item.declaration_id == key).inclusion == 'contextual'
    assert next(item for item in loaded.snapshot.declarations
                if item.declaration_id == 'public.background.music_memory_research').inclusion == 'phase'


def test_development_metadata_cannot_modify_core_or_accept_unknown_fields(tmp_path):
    payload = json.loads(RELEASE.read_text(encoding='utf-8'))
    core = next(row for row in payload['declarations'] if row['inclusion'] == 'core')
    core['development'] = [{'key':'new_interest', 'label':'合成主题', 'kind':'interest',
                            'baseline':'neutral', 'anchor':False}]
    path = tmp_path / 'core.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    assert load_persona(path).error_code.value == 'PERSONA_SCHEMA_INVALID'


@pytest.mark.parametrize('mode', [ReplyMode.TEXT_LETTER, ReplyMode.FUTURE_IM, ReplyMode.VOICE_REPLY])
def test_real_pipeline_selects_once_and_writer_keeps_the_frozen_persona(tmp_path, monkeypatch, mode):
    from tests.persona.test_reply_pipeline import _configured_v2_pipeline
    from reply_orchestrator import ReplyRequest, ReplyState
    monkeypatch.setenv('OLIVIA_REPLY_REVIEW_ENABLED', 'false')
    monkeypatch.setenv('OLIVIA_LETTER_CURRENT_TURN_INTERPRETATION', '0')
    path = tmp_path / 'persona.json'
    payload = json.loads(RELEASE.read_text(encoding='utf-8'))
    path.write_text(json.dumps(payload), encoding='utf-8')
    pipeline, _, bridge, provider = _configured_v2_pipeline(path)
    selected = next(row['statement'] for row in payload['declarations'] if row['declaration_id'] == 'anchor.reading')
    omitted = next(row['statement'] for row in payload['declarations'] if row['declaration_id'] == 'anchor.everyday_taste')
    calls = []
    async def select(messages, **kwargs):
        calls.append(messages)
        assert 'anchor.reading' in messages[-1]['content']
        assert 'anchor.current_piece' not in messages[-1]['content']
        next(row for row in payload['declarations'] if row['declaration_id'] == 'anchor.reading')['statement'] = '替换后的新版本不应进入本轮。'
        path.write_text(json.dumps(payload), encoding='utf-8')
        return SimpleNamespace(text=json.dumps({'selected_ids':[], 'dependencies':[], 'persona_ids':['anchor.reading']}))
    provider.complete_structured_scoped = select
    context = ReplyContext.create(mode, trusted_time=TrustedTime(NOW),
        **({'future_im_enabled':True} if mode is ReplyMode.FUTURE_IM else {}))
    result = asyncio.run(pipeline.run(ReplyRequest(content='你平时读什么？'), context))
    assert result.state is ReplyState.COMPLETED
    assert len(calls) == 1 and provider.calls == 1 and bridge.calls == 0
    wire = '\n'.join(message['content'] for message in provider.messages)
    assert selected in wire and omitted not in wire
    assert '替换后的新版本不应进入本轮。' not in wire
    assert 'anchor.current_piece' not in wire and 'public.background.music_memory_research' not in wire


def test_review_authority_cannot_reintroduce_phase_statements():
    from runtime.reply.reply_model_quality import _build_release_layer_authorities, _persona_review_profile
    value = load_persona(RELEASE).snapshot
    authority = _build_release_layer_authorities(value, mode='text_letter')
    wire = str(authority) + str(_persona_review_profile(RELEASE, 'text_letter'))
    for item in value.declarations:
        if item.inclusion == 'phase':
            assert item.statement not in wire


def test_review_uses_frozen_core_and_only_contextual_facts_retained_by_writer(tmp_path):
    from runtime.persona.persona_selection import snapshot_for_messages
    from runtime.reply.reply_model_quality import using_persona_snapshot, _persona_review_profile
    value = snapshot()
    messages = assemble_persona(value, CONTEXT, user_input='读什么？', max_units=20000,
        selected_declaration_ids=('taste.reading',)).to_messages()
    frozen = snapshot_for_messages(value, messages)
    assert {item.declaration_id for item in frozen.declarations} == {'trait.core', 'taste.reading'}
    with using_persona_snapshot(frozen):
        # No new file read may replace the writer's frozen personality.
        profile = _persona_review_profile(tmp_path / 'not-the-original-asset.json', 'text_letter')
    assert profile['status'] == 'READY'
    assert any(rule['statement'] == '她有自己的主见和边界。' for rule in profile['rules'])


def test_current_development_only_qualifies_its_own_dimension_without_rewriting_baseline():
    from runtime.persona.persona_selection import project_persona_selection
    from runtime.reply.reply_model_quality import _selected_persona_facts
    value = load_persona(RELEASE).snapshot
    original = next(item.statement for item in value.declarations if item.declaration_id == 'anchor.everyday_taste')
    development = {'version':3, 'items':[{'key':'spicy', 'label':'辣味', 'kind':'taste',
        'baseline':'avoid', 'anchor':True, 'stage':'willing_to_try', 'direction':'positive',
        'statement':'现在愿意偶尔尝一点微辣，仍不喜欢重辣。', 'source_ids':['synthetic:shared-meal']},
        {'key':'photography', 'label':'摄影', 'kind':'interest', 'baseline':'neutral', 'anchor':False,
         'stage':'trying', 'direction':'positive', 'statement':'在尝试摄影。', 'source_ids':['synthetic:photo']}]}
    messages = assemble_persona(value, CONTEXT, user_input='能吃辣了吗？', max_units=20000,
        selected_declaration_ids=()).to_messages()
    result = project_persona_selection(messages, value, 'text_letter', ('anchor.everyday_taste',),
        max_input_chars=20000, development=development)
    wire = '\n'.join(message['content'] for message in result)
    assert original in wire and development['items'][0]['statement'] in wire
    assert development['items'][1]['statement'] not in wire
    assert 'current_development' in _selected_persona_facts(result)
    assert '喜甜' in wire and '外婆做的糖醋排骨' in wire


def test_complete_catalog_reaches_selector_at_9000_with_recent_conversation():
    from runtime.memory.history_selection import select_history_messages
    from runtime.persona.persona_selection import contextual_catalog
    value = load_persona(RELEASE).snapshot
    # This is the selector packet's bound, independent from generation's core cost.
    messages = ({'role':'system','content':'核心人格固定。'},
        *({'role':'user' if i % 2 == 0 else 'assistant', 'content':('合成最近聊天内容。'*30)+str(i)} for i in range(6)),
        {'role':'user','content':'你平时读什么书？'})
    calls = []
    async def complete(packet_messages, **kwargs):
        packet = json.loads(packet_messages[-1]['content'])
        calls.append(packet)
        assert len(packet['persona_candidates']) == len(contextual_catalog(value, 'future_im'))
        assert len(packet['recent_dialogue']) >= 2
        assert sum(len(message['content']) for message in packet_messages) <= 9000
        return SimpleNamespace(text=json.dumps({'selected_ids':[], 'dependencies':[], 'persona_ids':['anchor.reading']}))
    output = asyncio.run(select_history_messages(messages, SimpleNamespace(complete_structured_scoped=complete),
        max_input_chars=9000, persona_snapshot=value, persona_mode='future_im'))
    assert len(calls) == 1 and 'anchor.reading' in str(output)

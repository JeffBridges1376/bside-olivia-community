import json
from types import SimpleNamespace

import pytest

from runtime.personal_chat.presentation import CURRENT
from runtime.reply.fact_attribution import prepare_dialogue_messages
from runtime.reply.jev_quality import _purpose_state, _confirmation_context, review_messages
from runtime.reply.reply_model_quality import _assembled_memory_evidence, _reference_objects


@pytest.mark.parametrize('metadata, expected', [
    (None, ('letter', 'text', False)),
    ({'channel': 'qq', 'incoming_format': 'text', 'voice_available': True}, ('qq', 'text', False)),
    ({'channel': 'wechat', 'incoming_format': 'voice'}, ('wechat', 'voice', True)),
])
def test_generation_and_review_share_actual_input_not_output_voice(metadata, expected):
    token = CURRENT.set(metadata)
    try:
        messages = prepare_dialogue_messages((
            {'role': 'system', 'content': '角色规则'},
            {'role': 'user', 'content': '语音听过了？我上次说要爬泰山，你记得几月吗？'},
        ), max_input_chars=10000)
    finally:
        CURRENT.reset(token)
    evidence = _assembled_memory_evidence(messages)
    blocks = [data for tag, data in _reference_objects(evidence)
              if tag == 'evidence_summary' and data.get('fragment_id') == 'current.input_evidence']
    assert len(blocks) == 1
    data = json.loads(blocks[0]['text'])
    assert (data['channel'], data['incoming_format'], data['voice_transcript_available']) == expected
    assert data['audio_waveform_available'] is False
    # Only trusted metadata describes modality; the user's question cannot create audio evidence.
    assert '泰山' not in blocks[0]['text']
    layer = SimpleNamespace(name='continuity_memory', allowed_codes=('MEMORY_FABRICATION',),
                            global_authority='', layer_authority='', runtime_authority='', question='')
    request = review_messages(layer, candidate='语音听过了，你声音没什么气。',
        current_user_input=messages[-1]['content'], mode='text_letter',
        memory_evidence={'assembled_memory': evidence})
    state = _purpose_state(layer, request, [])
    assert any(item['value'].get('fragment_id') == 'current.input_evidence'
               for item in state['input']['selected_memory'])
    # The second, independent decision must see the same transport facts.
    confirmation = _confirmation_context('continuity_fact', {'continuity_memory': state['input']})
    assert confirmation['selected_memory'] == state['input']['selected_memory']
    assert confirmation['current_user_input'] == messages[-1]['content']


def test_recall_question_has_separate_current_claim_and_historical_support_rules():
    from runtime.reply.fact_attribution import FACT_ATTRIBUTION_BOUNDARY
    from runtime.reply.jev_quality import _CODE_RULES
    assert '当前提问' in FACT_ATTRIBUTION_BOUNDARY
    assert '独立的历史原话' in FACT_ATTRIBUTION_BOUNDARY
    rule = _CODE_RULES['MEMORY_FABRICATION']
    assert '当前提问' in rule['what']
    assert '听过' in rule['what']
    assert '独立' in rule['not_for']

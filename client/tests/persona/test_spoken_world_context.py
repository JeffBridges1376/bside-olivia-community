import asyncio
import json
from pathlib import Path

import pytest

from persona_assembly import UntrustedFragment
from reply_orchestrator import ReplyRequest, ReplyState
from runtime.media.voice_direction import TextOnlyVoicePlan
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_media import ReplyMediaError, render_reply_audio, render_reply_video
from tests.persona.test_expression_context import NOW
from tests.persona.test_reply_pipeline import ROOT, _configured_v2_pipeline


@pytest.mark.parametrize('mode', [ReplyMode.VOICE_REPLY, ReplyMode.SPOKEN_VIDEO])
def test_actual_spoken_pipeline_freezes_episode_affect_and_sends_only_frozen_speech(mode, tmp_path, monkeypatch):
    from runtime import remote_pipeline
    monkeypatch.setenv('OLIVIA_REPLY_REVIEW_ENABLED', 'false')
    monkeypatch.setenv('OLIVIA_LETTER_CURRENT_TURN_INTERPRETATION', '0')
    pipeline, _, bridge, provider = _configured_v2_pipeline(ROOT/'linli_character/persona_release_v2.json')
    episode = {'source_id': 'practice:1', 'occurred_at': NOW.isoformat(),
               'process': [{'obstacle': '慢练仍错音', 'response': '拆成两小节练习', 'outcome': '终于弹顺'}],
               'result': {'status': 'completed', 'detail': '困难段落已弹顺'}}
    world = {'kind': 'character_life_reference', 'recent_episodes': [episode]}
    emotion = {'reaction_subject': 'character', 'interpretation_only': True, 'reactions': [],
               'concerns': [], 'reported_affects': [], 'current_affect': {
                   'status': 'available', 'label': 'pleased', 'as_of': NOW.isoformat(),
                   'reason': '困难段落已弹顺', 'basis': {'kind': 'episode'}}}
    monkeypatch.setattr(bridge.adapter, 'daily_life_fragments', lambda *a, **k:
                        (UntrustedFragment('linli.daily-life', json.dumps(world, ensure_ascii=False)),))
    async def affect(*a, **k):
        return emotion
    monkeypatch.setattr(bridge.adapter, 'prepare_character_emotion', affect)
    result = asyncio.run(pipeline.run(ReplyRequest(content='说说你今天练琴怎么样了', max_input_chars=30000),
        ReplyContext.create(mode, trusted_time=TrustedTime(NOW))))
    assert result.state is ReplyState.COMPLETED
    wire = '\n'.join(m['content'] for m in provider.messages)
    assert '拆成两小节练习' in wire and 'current_affect' in wire and 'pleased' in wire
    assert result.expression_context['world']['recent_episodes'] == [episode]
    assert result.expression_context['emotion']['current_affect'] == emotion['current_affect']
    # Later world changes cannot rewrite the speech already generated for this turn.
    episode['result']['detail'] = '后来又遇到新问题'
    assert '后来' not in json.dumps(result.expression_context, ensure_ascii=False)
    calls = []
    monkeypatch.setattr(remote_pipeline, 'generate', lambda kind, data, *a, **k: calls.append((kind, data)) or {})
    kwargs = dict(tts_config_path=Path(), voice_performance_plan=TextOnlyVoicePlan(result.text),
                  environment={'OLIVIA_GPU_ROUTE': 'remote'})
    if mode is ReplyMode.VOICE_REPLY:
        render_reply_audio(result.text, tmp_path/'out.wav', **kwargs)
    else:
        render_reply_video(result.text, tmp_path/'out.mp4', visual_config_path=Path(), worker_path=Path(),
                           adaptive_delivery=True, **kwargs)
    assert calls[0][1]['text'] == result.text
    assert calls[0][1]['voice_plan'] == {'reply_text': result.text}
    assert 'emotion' not in calls[0][1] and 'short_instruction' not in calls[0][1]


def test_remote_video_rejects_another_turn_voice_plan_before_provider(tmp_path, monkeypatch):
    from runtime import remote_pipeline
    calls = []
    monkeypatch.setattr(remote_pipeline, 'generate', lambda *a, **k: calls.append(a))
    with pytest.raises(ReplyMediaError, match='VOICE_DIRECTION_TEXT_MISMATCH'):
        render_reply_video('本轮已冻结台词。', tmp_path/'out.mp4', tts_config_path=Path(),
                           visual_config_path=Path(), worker_path=Path(), adaptive_delivery=True,
                           voice_performance_plan=TextOnlyVoicePlan('上一轮台词。'),
                           environment={'OLIVIA_GPU_ROUTE': 'remote'})
    assert calls == []

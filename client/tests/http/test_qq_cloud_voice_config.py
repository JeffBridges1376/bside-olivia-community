import asyncio
from types import SimpleNamespace
import pytest
from runtime.personal_chat.backend import prepare_chat_audio
from runtime.reply.reply_media import render_reply_audio
from runtime.media.voice_direction import TextOnlyVoicePlan

def test_cloud_qq_speech_needs_no_local_tts_config(monkeypatch,tmp_path):
    import runtime.remote_pipeline as remote
    monkeypatch.delenv('OLIVIA_TTS_CONFIG',raising=False)
    monkeypatch.setenv('OLIVIA_GPU_ROUTE','remote')
    seen=[]
    def generate(kind,data,path,**kwargs):
        seen.append((kind,data,kwargs['environment']['OLIVIA_MEDIA_CHANNEL']))
        path.write_bytes(b'synthetic-audio')
        return {'duration_seconds':3}
    monkeypatch.setattr(remote,'generate',generate)
    result=asyncio.run(prepare_chat_audio(SimpleNamespace(render_reply_audio=render_reply_audio),'我在呢',tmp_path/'voice.wav'))
    assert result['duration_seconds']==3
    assert seen==[('tts',{'text':'我在呢','voice_plan':TextOnlyVoicePlan('我在呢').to_dict()},'qq')]

def test_missing_local_voice_config_remains_explicit(monkeypatch,tmp_path):
    monkeypatch.delenv('OLIVIA_TTS_CONFIG',raising=False)
    monkeypatch.delenv('OLIVIA_GPU_ROUTE',raising=False)
    with pytest.raises(RuntimeError,match='PERSONAL_CHAT_TTS_UNAVAILABLE'):
        asyncio.run(prepare_chat_audio(SimpleNamespace(render_reply_audio=render_reply_audio),'hello',tmp_path/'voice.wav'))

def test_real_readiness_gate_advertises_remote_speech_to_qq_decision(monkeypatch,tmp_path):
    from letter_triage import _voice_reply_configured
    from tests.http.test_chat_jev_decision import server_fixture,pipeline_result
    from runtime.personal_chat.backend import generate
    from runtime.personal_chat.events import PersonalMessage
    monkeypatch.delenv('OLIVIA_TTS_CONFIG',raising=False)
    monkeypatch.setenv('OLIVIA_GPU_ROUTE','remote')
    monkeypatch.setenv('OLIVIA_GPU_API_URL','http://127.0.0.1:9')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY','synthetic')
    server,row,seen,_,audio=server_fixture(monkeypatch,tmp_path,pipeline_result(delivery='audio_speech'))
    server._voice_reply_configured=_voice_reply_configured
    asyncio.run(generate(server,PersonalMessage('qq','b','u','1','我还想听你声音'),row))
    assert 'audio_speech' in seen[0]['semantic_kinds']
    assert row['requested_format']=='voice' and audio

@pytest.mark.parametrize('missing',['OLIVIA_GPU_API_URL','OLIVIA_GPU_API_KEY'])
def test_remote_readiness_does_not_claim_unconfigured_service(monkeypatch,missing):
    from letter_triage import _voice_reply_configured
    env=dict(OLIVIA_GPU_ROUTE='remote',OLIVIA_GPU_API_URL='http://127.0.0.1:9',OLIVIA_GPU_API_KEY='synthetic')
    env.pop(missing)
    assert not _voice_reply_configured(env)

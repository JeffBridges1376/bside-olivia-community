import asyncio
import json
import pytest
from runtime.reply.companion_decision import FrozenCompanionTurn,FrozenCompanionDecision,JevDecisionPort,CompanionDecisionError
from tests.persona.test_companion_decision import input_args,envelope,sidecar


@pytest.mark.parametrize('scene',['rain_room','quiet_room','bedside_reading','desk_writing','seaside'])
def test_speech_intent_uses_existing_http_call_and_replays_frozen(sidecar,scene):
    state=sidecar;url=state['url']
    state['body']={**envelope(),'speech_request':dict(mode='asmr_story',target_seconds=180,continuation=True,ambience_scene=scene)}
    turn=FrozenCompanionTurn.create(**input_args(),speech_enabled=True)
    result=asyncio.run(JevDecisionPort(url).decide(turn))
    assert result.error_code is None
    record=result.decision.record()
    assert FrozenCompanionDecision.from_record(turn,record).record()==record
    assert len(state['calls'])==1
    packet=json.loads(state['calls'][0][2]);assert packet['speech_experience'] is True
    assert packet['input']==turn.input


def test_enabled_turn_rejects_old_or_invalid_speech_response():
    turn=FrozenCompanionTurn.create(**input_args(),speech_enabled=True)
    for speech in ('missing',dict(mode='asmr',target_seconds=True,continuation=False)):
        data=envelope()
        if speech!='missing':data['speech_request']=speech
        with pytest.raises((CompanionDecisionError,ValueError)):
            FrozenCompanionDecision.from_response(turn,data)


@pytest.mark.parametrize('was_enabled',[False,True])
def test_cached_decision_keeps_original_speech_scope_after_capability_change(monkeypatch,was_enabled):
    from types import SimpleNamespace
    from runtime.reply.companion_decision import CompanionDecisionResult
    from runtime.reply.companion_runtime import prepare_decision
    from tests.persona.test_companion_decision import NOW
    monkeypatch.setattr('runtime.reply.companion_runtime._decision_context',
        lambda *a:[dict(event_id='previous',role='assistant',text='上次聊到摄影。')])
    calls=[]
    async def decide(turn):
        calls.append(turn)
        response=envelope()
        if turn.speech_enabled:response['speech_request']=None
        return CompanionDecisionResult(decision=FrozenCompanionDecision.from_response(turn,response))
    port=SimpleNamespace(decide=decide,profile='full')
    args=dict(source_id='current',input_revision=0,as_of=NOW,kinds=['text'])
    old=asyncio.run(prepare_decision(port,[],'拍了第一张照片，有点开心。',**args,speech_enabled=was_enabled))
    record=old.record()
    if not was_enabled:record.pop('speech_request',None)  # A record from the previous client.
    restored=asyncio.run(prepare_decision(port,[],'拍了第一张照片，有点开心。',**args,
                                         speech_enabled=not was_enabled,cached=record))
    assert restored.record()==record
    assert len(calls)==1

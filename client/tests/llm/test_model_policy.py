import json

from runtime.model_policy import aliases, encode_chat, encode_decision
from llm_gateway import _RELAY_HOST


def test_only_managed_control_fields_become_references():
    original,policy_id=aliases()[0]
    payload={'messages':[{'role':'system','content':original},{'role':'user','content':original}]}
    result=encode_chat(payload,'https://'+_RELAY_HOST+'/v1')
    assert result['messages'][0]['content']=='[[olivia-policy:v1:'+policy_id+']]'
    assert result['messages'][1]['content']==original
    assert payload['messages'][0]['content']==original
    assert encode_chat(payload,'https://byok.example/v1')==payload


def test_persona_history_world_stay_verbatim_and_jev_criteria_keep_ids():
    original,policy_id=aliases()[0]
    source='<history>'+json.dumps([{'content':original}])+'</history>'
    body={'messages':[{'role':'system','content':source+'<forbidden>'+json.dumps([original])+'</forbidden>'}]}
    assert source in encode_chat(body,'https://'+_RELAY_HOST+'/v1')['messages'][0]['content']
    packet={'state':{'contract':original,'incoming':original,'history':[{'instructions':original}]},
            'questions':{'choice':{'instructions':original,'criteria':{'yes':'是','no':'否'}}},'purpose':'synthetic'}
    effective=encode_decision(packet,'https://'+_RELAY_HOST+'/v1/companion/semantic-decisions')
    assert effective['state']['incoming']==original
    assert effective['state']['history']==packet['state']['history']
    assert effective['questions']['choice']['criteria']==packet['questions']['choice']['criteria']

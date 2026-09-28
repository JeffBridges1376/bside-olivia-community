from runtime.reply.web_search import apply_search
from original_client_relay_api import RELAY_BASE

def test_search_only_user_facing_relay_scopes():
    body={'model':'qwen3.7-flash','messages':[{'role':'user','content':'你好'}]}
    enabled=apply_search(body,RELAY_BASE,'personal_chat_json')
    assert enabled['enable_search'] is True
    assert 'enable_search' not in body
    for scope in ['recall_check','background_reasoning','proactive_planning',None]:
        assert apply_search(body,RELAY_BASE,scope) is body
    assert apply_search(body,'https://example.org','personal_chat_json') is body

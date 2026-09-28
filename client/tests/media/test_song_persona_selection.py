"""The real lyric planning entrance freezes and selects persona once."""
import json
from pathlib import Path
from types import SimpleNamespace

from llm_gateway import GatewayConfig
from tests.http.test_expression_context_routes import run_isolated
from tests.media.test_song_content_pipeline import RecordingGateway, _payload


ROOT = Path(__file__).resolve().parents[2]


def test_actual_letter_adapter_song_selects_frozen_asset_world_and_clock(tmp_path):
    run_isolated(tmp_path, r'''
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
import local_server as server
from llm_gateway import GatewayConfig, GatewayRequestScope
from persona_assembly import UntrustedFragment
from runtime.media.song_content import plan_song_content
from tests.media.test_song_content_pipeline import _payload
root=server._state_root()
payload=json.loads(Path(server.letters_adapter.persona_v2_path).read_text(encoding='utf-8'))
asset=root/'song-frozen-persona.json';asset.write_text(json.dumps(payload),encoding='utf-8')
original=next(d['statement'] for d in payload['declarations'] if d['declaration_id']=='anchor.reading')
clock=[];NOW=datetime(2026,9,27,2,tzinfo=timezone.utc)
def now():
    clock.append(True)
    return NOW+timedelta(days=len(clock)-1)
adapter=server.LetterAdapter(GatewayConfig(provider='mock',persona_v2_file=str(asset)),now=now)
development={'version':'frozen-development','as_of':NOW.isoformat(),'items':[{
    'key':'reading','label':'阅读','kind':'interest','baseline':'like','anchor':True,
    'stage':'growing','direction':'positive','statement':'最近对记忆主题随笔的兴趣略有增加。','source_ids':['day:reading']}]}
life_reads=[]
def life(content, *, recent_fragments=None, now=None):
    life_reads.append(now)
    return (UntrustedFragment('linli.daily-life',json.dumps({'kind':'character_life_reference',
        'current':None,'stale':True,'threads':[],'character_development':development},ensure_ascii=False)),)
adapter.daily_life_fragments=life
initial=[];original_assemble=adapter.reply_context_messages
def assemble(*args,**kwargs):
    assert kwargs['as_of']==NOW
    assert kwargs['persona_snapshot'].status=='READY'
    result=original_assemble(*args,**kwargs);initial.append(result)
    assert original not in str(result)
    return result
adapter.reply_context_messages=assemble
calls=[]
class Model:
    # Deliberately different from the active adapter's selected asset.
    config=GatewayConfig(provider='mock',persona_v2_file=str(root/'wrong-persona.json'))
    async def complete_structured_scoped(self,messages,**kwargs):
        calls.append(('select',messages,kwargs))
        packet=json.loads(messages[-1]['content'])
        assert original in str(packet['persona_candidates'])
        assert all(p['id']!='anchor.current_piece' for p in packet['persona_candidates'])
        next(d for d in payload['declarations'] if d['declaration_id']=='anchor.reading')['statement']='后来改写的人设不应进入本轮。'
        asset.write_text(json.dumps(payload),encoding='utf-8')
        adapter.persona_v2_path=root/'now-missing.json'
        development['items'][0]['statement']='后来变化的发展视图不应进入本轮。'
        return SimpleNamespace(text=json.dumps({'selected_ids':[],'dependencies':[],'persona_ids':['anchor.reading']}))
    async def complete_scoped(self,messages,**kwargs):
        calls.append(('lyrics',messages,kwargs))
        return SimpleNamespace(text=json.dumps(_payload()))
model=Model()
plan_song_content('能写一首跟阅读有关的歌吗？','好，我试着写。',40,gateway=model,reply_adapter=adapter)
assert [c[0] for c in calls]==['select','lyrics']
assert calls[0][2]['scope']==GatewayRequestScope.RECALL_CHECK
assert calls[1][2]['scope']==GatewayRequestScope.SONG_CONTENT
assert len(clock)==1 and life_reads==[NOW]
wire='\n'.join(m['content'] for m in calls[-1][1])
assert original in wire and '最近对记忆主题随笔的兴趣略有增加。' in wire
assert '后来改写' not in wire and '后来变化' not in wire
assert 'anchor.current_piece' not in wire and 'public.background.music_memory_research' not in wire
assert 'constitution.autonomy' in wire
assert sum(len(m['content']) for m in calls[-1][1])<=model.config.max_input_chars
''')


def test_standalone_song_core_only_then_same_selector_adds_context(tmp_path, monkeypatch):
    from runtime.media import song_content
    asset=tmp_path/'persona.json'
    payload=json.loads((ROOT/'linli_character/persona_release_v2.json').read_text(encoding='utf-8'))
    asset.write_text(json.dumps(payload),encoding='utf-8')
    original=next(d['statement'] for d in payload['declarations'] if d['declaration_id']=='anchor.reading')
    initial=[]
    prepare=song_content._planning_messages
    def planning(*args,**kwargs):
        messages=prepare(*args,**kwargs);initial.append(messages)
        return messages
    monkeypatch.setattr(song_content,'_planning_messages',planning)
    class Model(RecordingGateway):
        selections=0
        async def complete_structured_scoped(self,messages,**kwargs):
            self.selections+=1
            next(d for d in payload['declarations'] if d['declaration_id']=='anchor.reading')['statement']='wrong new asset'
            asset.write_text(json.dumps(payload),encoding='utf-8')
            return SimpleNamespace(text=json.dumps({'selected_ids':[],'dependencies':[],'persona_ids':['anchor.reading']}))
    gateway=Model(json.dumps(_payload()),config=GatewayConfig(provider='mock',persona_v2_file=str(asset)))
    song_content.plan_song_content('写首关于阅读的歌','好',40,gateway=gateway)
    assert gateway.selections==1 and len(gateway.calls)==1
    assert original not in str(initial[0])
    assert original in str(gateway.calls[0][0]) and 'wrong new asset' not in str(gateway.calls[0][0])

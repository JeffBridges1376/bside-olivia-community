import asyncio
from types import SimpleNamespace

from PIL import Image

from runtime import image_reply


def test_photo_submits_facts_and_uses_server_plan_without_local_directors(tmp_path,monkeypatch):
    calls=[]
    class API:
        url='https://gpu.example'
        token='synthetic'
        def __init__(self,*args):
            pass
        async def request(self,action,data):
            return {'server_media_planning':True}
        async def generate(self,kind,data,path,**kwargs):
            calls.append(data)
            assert 'prompt' not in data and 'media_request' in data
            Image.new('RGB',(675,900)).save(path)
            kwargs['validate'](path)
            return {'task_id':'synthetic-task','stage':'completed',
                    'media_plan':{'prompt':'Synthetic portrait','photo_type':'portrait','room':'bedroom','time_of_day':'night'}}
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration',API)
    async def forbidden(*args,**kwargs):
        raise AssertionError('client planner must not run')
    monkeypatch.setattr(image_reply,'_jev_photo_plan',forbidden)
    monkeypatch.setattr(image_reply,'_photo_reference',lambda *a:{'world_current_location':'家里'})
    async def observation(*args,**kwargs):
        return {'source':'generated'}
    monkeypatch.setattr('runtime.image_understanding.describe_image',observation)
    server=SimpleNamespace(video_reply_settings_store=SimpleNamespace(image_snapshot=lambda:{'enabled':True,'resolution':'1K'}),
                           _media_root=lambda:tmp_path,_persist_store_state=lambda:None,PORT=1234)
    row={'letter_id':'synthetic-letter'}
    asyncio.run(image_reply._prepare_once(server,row,'合成来信','合成回信',channel='qq'))
    assert row['image_status']=='COMPLETED'
    assert row['image_plan']['room']=='bedroom'
    assert len(calls)==1
    assert calls[0]['media_request']['incoming']=='合成来信'

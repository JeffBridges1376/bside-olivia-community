"""The actual adapter reads only durable input, independently of reply delivery."""
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_letter_and_merged_qq_emotion_use_immutable_receipts(tmp_path):
    script = r'''
import asyncio,json
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import local_server as server
from runtime.memory.received_user_originals import received_originals
now=datetime(2026,9,27,2,tzinfo=timezone.utc)
old=now-timedelta(minutes=1)
first={'letter_id':'first','channel':'qq','binding_id':'test-account','source_messages':{'m1':'请慢慢来'},'content':'请慢慢来','life_received_at':old.isoformat(),'delivery_status':'FAILED','reply_text':'没有送达的草稿'}
merged={**first,'letter_id':'merged','source_messages':{'m1':'请慢慢来','m2':'不用急'},'content':'请慢慢来\n不用急','life_received_at':now.isoformat()}
unrelated={**first,'letter_id':'different','source_messages':{'m3':'不在本轮的消息'},'content':'不在本轮的消息'}
letter={'letter_id':'letter','content':'不着急回复','life_received_at':now.isoformat(),'reply_revision':1,'letter_status':'FAILED','reply_text':'这段不能评价'}
server.store.personal_chats[:]=[first,merged,unrelated]
server.store.letters[:]=[letter]
class Emotion:
    def __init__(self): self.seen=[]
    async def evaluate_received(self,receipts,*,now):
        self.seen.append([(r.source_id,r.user_message,r.occurred_at.isoformat()) for r in receipts])
        return self.view(now)
    def view(self,now): return {'interpretation_only':True,'reaction_subject':'character','reactions':[]}
emotion=Emotion()
server.letters_adapter.daily_life=SimpleNamespace(emotion=emotion)
async def call(source,text):
    token=server._CURRENT_LETTER_MEMORY_SOURCE.set(source)
    try: return await server.letters_adapter.prepare_character_emotion(text,now=now)
    finally: server._CURRENT_LETTER_MEMORY_SOURCE.reset(token)
async def main():
    await call('reply:merged:1',merged['content'])
    await call('reply:letter:2',letter['content'])
    await call('reply:merged:1','这段并不是用户原话')
    await call('reply:merged:1',None)
    await call(None,'假的当前输入')
asyncio.run(main())
print(json.dumps(emotion.seen,ensure_ascii=True))
'''
    env = {**os.environ, 'OLIVIA_LOCAL_DATA_ROOT': str(tmp_path), 'OLIVIA_LLM_PROVIDER': 'none',
           'OLIVIA_MEMORY_ENABLED': '0', 'PYTHONUTF8': '1', 'PYTHONPATH': str(ROOT)}
    env.pop('OLIVIA_PRIVATE_WORLD_DB', None)
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    assert [[r[1] for r in batch] for batch in seen] == [['请慢慢来', '不用急'], ['不着急回复'], []]
    assert seen[0][0][2] == '2026-09-27T01:59:00+00:00'
    assert all(r[0].startswith('received-user:') for batch in seen for r in batch)

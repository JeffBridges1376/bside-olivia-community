"""Synthetic route checks: bind the adopted view to the actual reply body."""
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def run_isolated(tmp_path, script):
    env = {**os.environ, 'OLIVIA_LOCAL_DATA_ROOT': str(tmp_path), 'OLIVIA_LLM_PROVIDER': 'none',
           'OLIVIA_MEMORY_ENABLED': '0', 'PYTHONUTF8': '1', 'PYTHONPATH': str(ROOT)}
    env.pop('OLIVIA_PRIVATE_WORLD_DB', None)
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_spoken_letter_route_passes_exact_received_original_to_actual_adapter(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import local_server as server
from reply_orchestrator import ReplyResult, ReplyState
from runtime.memory.received_user_originals import received_originals
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer

now = datetime(2026, 9, 27, 2, tzinfo=timezone.utc)
raw = '我已经醒了。  \n'
letter = {'letter_id': 'synthetic-spoken', 'content': raw,
          'life_received_at': now.isoformat(), 'reply_revision': 3}
server.store.letters[:] = [letter]
server.store.personal_chats[:] = []
server._persist_store_state()
server.store.letters.clear()
server._load_store_state()
letter = server.store.letters[0]
receipts = received_originals([letter])
seen, requests = [], []
class Emotion:
    async def evaluate_received(self, batch, *, now):
        seen.append(tuple(batch))
        return {'reaction_subject': 'character', 'interpretation_only': True, 'reactions': []}
server.letters_adapter.daily_life = SimpleNamespace(emotion=Emotion())
async def no_world_fragments(*args, **kwargs): return ()
server.letters_adapter.prepare_daily_life_fragments = no_world_fragments
server.letters_adapter._now = lambda: now
server._current_life_rhythm = lambda: {}
class Engine:
    gateway = SimpleNamespace(adapter=server.letters_adapter)
    async def run(self, request):
        requests.append(request)
        return ReplyResult(request.request_id, ReplyState.COMPLETED, text='那就继续聊。')
server.reply_pipeline = ReplyPipeline(Engine(), reviewer=NullReviewer(),
    rewriter=UnavailableRewriter(), discover_runtime_ports=False)
result = asyncio.run(server._run_reply_pipeline_for_letter(letter, raw, 'spoken_video', idempotency_key=None))
assert result.state is ReplyState.COMPLETED
assert len(requests) == 1 and requests[0].received_user_text == raw
assert '<ordinary_video_reply_constraints>' in requests[0].content
assert raw not in requests[0].content
assert seen == [receipts] and len(receipts) == 1
assert receipts[0].user_message == raw and receipts[0].source_id.startswith('received-user:letter:')
assert server._CURRENT_LETTER_MEMORY_SOURCE.get() is None
''')


def test_generate_letter_binds_body_after_signature_removal_and_revision_update(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio, hashlib, json
from types import SimpleNamespace
import local_server as server
from reply_orchestrator import ReplyResult, ReplyState
from runtime import image_reply
from runtime.reply.character_emotion_context import checked_expression_context
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer

body = '醒了就先喝点水。\n\n今天不用赶。'
raw_reply = body + '\n\n林离\n[[signature:林离]]'
letter = {'letter_id': 'synthetic-letter', 'content': '我醒了', 'reply_revision': 7,
          'reply_text': '上一稿', 'private_world_delivery_id': 'synthetic-letter:7',
          'image_reply_settings': {'enabled': False}}
server.store.letters[:] = [letter]
server.store.personal_chats[:] = []
server.daily_life_runtime = None
server.letters_adapter.daily_life = None
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *args: None
server._commit_private_world_letter = lambda row: False
server.letters_adapter.remember_conversation = lambda *args: None
image_reply.schedule = lambda *args: None
class Engine:
    gateway = SimpleNamespace(adapter=server.letters_adapter)
    async def run(self, request):
        return ReplyResult(request.request_id, ReplyState.COMPLETED, text=raw_reply)
server.reply_pipeline = ReplyPipeline(Engine(), reviewer=NullReviewer(),
    rewriter=UnavailableRewriter(), discover_runtime_ports=False)
assert asyncio.run(server.generate_reply(letter['letter_id'], letter['content']))
assert letter['reply_text'] == body and letter['reply_signature'] == '林离'
assert letter['reply_revision'] == 8
bound = checked_expression_context(letter)
assert bound is not None and bound['binding']['reply_revision'] == letter['reply_revision']
assert bound['binding']['reply_sha256'] == hashlib.sha256(body.encode()).hexdigest()
assert bound['binding']['reply_sha256'] != hashlib.sha256(raw_reply.encode()).hexdigest()
saved = json.loads((server._state_root() / 'state.json').read_text(encoding='utf-8'))['letters'][0]
assert checked_expression_context(saved) == bound
''')


def test_chat_backend_binds_decoded_body_with_notice_before_service_persists(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio, hashlib, json
from contextvars import ContextVar
from datetime import datetime, timezone
from types import SimpleNamespace
import persona_loader
from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.mailbox_notice import NOTICE_TEXT
from runtime.personal_chat.service import PersonalChatService
from runtime.reply.character_emotion_context import checked_expression_context, freeze_expression_context
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_pipeline import PipelineResult
from reply_orchestrator import ReplyState
from tests.http.test_personal_chat_decision import envelope

now = datetime(2026, 9, 27, 2, tzinfo=timezone.utc)
context = ReplyContext.create(ReplyMode.FUTURE_IM, trusted_time=TrustedTime(now), future_im_enabled=True)
persona_loader.load_persona = lambda _: SimpleNamespace(snapshot=SimpleNamespace(status='READY'))
rows, saved, generated = [], [], []
raw_reply = envelope(text='  那就继续聊 [QQ表情]  ')
snapshot = freeze_expression_context('synthetic-chat', now, emotion={
    'reaction_subject': 'character', 'interpretation_only': True, 'reactions': []})
async def run(request, context):
    return PipelineResult(request.request_id, ReplyState.COMPLETED,
                          text=raw_reply, expression_context=snapshot)
def persist():
    saved.append(json.loads(json.dumps(rows)))
server = SimpleNamespace(
    letters_adapter=SimpleNamespace(config=SimpleNamespace(persona_v2_enabled=True, max_input_chars=50000),
        persona_v2_path='synthetic', build_reply_context=lambda *args, **kwargs: context),
    _llm_runtime_ready=lambda _: True, daily_life_runtime=object(),
    _official_history_private_world_available=lambda: True,
    MEMORY_READY_REPLY_TIMEOUT_SECONDS=1, _conversation_memory_ready_for_reply=lambda: True,
    video_reply_settings_store=SimpleNamespace(image_snapshot=lambda: {'enabled': False}),
    _persist_store_state=persist,
    _CURRENT_LETTER_MEMORY_SOURCE=ContextVar('synthetic_source'),
    _CURRENT_LETTER_RECEIPT=ContextVar('synthetic_receipt'),
    store=SimpleNamespace(personal_chats=rows, letters=[{
        'letter_id': 'synthetic-unread', 'origin': 'proactive', 'letter_status': 'COMPLETED',
        'published_at': 10, 'is_read': 0}]),
    supports_scoped_reasoning=lambda _: False, _reply_pipeline_timeout_seconds=lambda _: 1,
    reply_pipeline=SimpleNamespace(run=run))
async def generate(event, row):
    text = await backend.generate(server, event, row)
    generated.append((text, json.loads(json.dumps(row))))
    return text
async def send(text):
    pass
async def commit(row):
    pass
service = PersonalChatService(rows, persist, generate, commit, {'qq': ('bot', 'owner')})
asyncio.run(service.handle(PersonalMessage('qq', 'bot', 'owner', '1', '继续聊'), send))
body = '那就继续聊\n\n' + NOTICE_TEXT
text, draft = generated[0]
assert text == body and draft['mailbox_notice_letter_id'] == 'synthetic-unread'
draft['reply_text'] = text
bound = checked_expression_context(draft)
assert bound is not None and bound['emotion'] == snapshot['emotion']
assert bound['view_sha256'] == snapshot['view_sha256'] and snapshot['binding'] is None
assert bound['binding']['reply_sha256'] == hashlib.sha256(body.encode()).hexdigest()
assert bound['binding']['reply_sha256'] != hashlib.sha256(raw_reply.encode()).hexdigest()
persisted_draft = next(batch[0] for batch in saved if batch and batch[0]['delivery_status'] == 'GENERATED')
assert persisted_draft['reply_text'] == body and checked_expression_context(persisted_draft) == bound
''')


def test_failed_letter_regeneration_clears_previous_expression_binding(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio, json
from datetime import datetime, timezone
from types import SimpleNamespace
import local_server as server
from reply_orchestrator import ReplyState
from runtime.reply.character_emotion_context import freeze_expression_context, store_expression_context, checked_expression_context
from runtime.reply.reply_pipeline import PipelineResult

letter = {'letter_id': 'synthetic-retry', 'content': '我醒了', 'reply_revision': 7,
          'reply_text': '上一稿', 'private_world_delivery_id': 'synthetic-retry:7'}
store_expression_context(letter, freeze_expression_context('previous-draft', datetime.now(timezone.utc)), letter['reply_text'])
assert checked_expression_context(letter) is not None
server.store.letters[:] = [letter]
server.store.personal_chats[:] = []
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *args: None
async def failed(request, context):
    return PipelineResult(request.request_id, ReplyState.FAILED, error_code='LLM_UNAVAILABLE')
server.reply_pipeline = SimpleNamespace(run=failed)
assert not asyncio.run(server.generate_reply(letter['letter_id'], letter['content']))
assert letter['letter_status'] == 'FAILED' and letter['reply_text'] == '上一稿'
assert 'expression_context' not in letter
saved = json.loads((server._state_root() / 'state.json').read_text(encoding='utf-8'))['letters'][0]
assert 'expression_context' not in saved
''')

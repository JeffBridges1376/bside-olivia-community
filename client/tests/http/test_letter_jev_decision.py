"""Development letter routing persists Jev decisions before any body or media."""
from tests.http.test_expression_context_routes import run_isolated


def test_jev_preview_photo_reaches_native_attachment_scheduler(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
import local_server as server
from runtime import image_reply
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.persona.test_jev_pipeline import Port, plan
from tests.persona.test_reply_semantic_wiring import Engine
from letter_triage import TriageResult
row = {'letter_id': 'jev-preview-image', 'content': '看看明天的穿搭',
       'image_reply_settings': {'enabled': True},
       'route_preflight': TriageResult('normal','text_letter','jev_no_explicit_media','completed',True).to_dict()}
old = {'plan': plan(kind='image')}
old['plan']['resolution'].update(status='unsupported', blocked_steps=['s1'])
row.update(error_code='JEV_PLAN_UNSUPPORTED', companion_decision=old)
server.store.letters[:] = [row]
server.store.personal_chats[:] = []
server.daily_life_runtime = None
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *a: None
server._commit_private_world_letter = lambda row: False
server.letters_adapter.remember_conversation = lambda *a: None
scheduled = []
image_reply.schedule = lambda server, row: scheduled.append(row['image_status'])
port = Port(plan(kind='image'))
server.reply_pipeline = ReplyPipeline(Engine('给你看看搭配。'), reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
    discover_runtime_ports=False, companion_decision_port=port)
assert asyncio.run(server.generate_reply(row['letter_id'], row['content']))
assert scheduled == ['PENDING']
assert row['reply_mode'] == 'text_letter'
assert row['input_revision'] == 1 and row['superseded_companion_decision'] == old
assert port.turns[0].input['capabilities']['kinds'] == ['text', 'image']
assert row['companion_delivery'] == 'image'
''')


def test_native_no_reply_persists_without_committing_or_scheduling(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
from types import SimpleNamespace
import local_server as server
from runtime import image_reply
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.persona.test_jev_pipeline import Port, plan
from tests.persona.test_reply_semantic_wiring import Engine

row = {'letter_id': 'jev-native', 'content': '这条不用回复', 'image_reply_settings': {'enabled': True}}
server.store.letters[:] = [row]
server.store.personal_chats[:] = []
server.daily_life_runtime = None
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *a: None
calls = []
server._commit_private_world_letter = lambda row: calls.append('world')
server.letters_adapter.remember_conversation = lambda *a: calls.append('memory')
image_reply.schedule = lambda *a: calls.append('photo')
engine, port = Engine('must not generate'), Port(plan(timing='no_reply'))
server.reply_pipeline = ReplyPipeline(engine, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
    discover_runtime_ports=False, companion_decision_port=port)
assert asyncio.run(server.generate_reply(row['letter_id'], row['content']))
assert row['letter_status'] == 'SKIPPED' and row.get('reply_text', '') == ''
assert row['companion_timing'] == 'no_reply' and not calls and not engine.requests
assert row['companion_decision']['source_id_map'] == {'t1': 'reply:jev-native:user'}
server.store.letters.clear()
server._load_store_state()
assert server.store.letters[0]['companion_decision']['plan']['proposal']['timing'] == 'no_reply'
''')


def test_native_writer_failure_reuses_saved_jev_on_retry(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
import local_server as server
from reply_orchestrator import ReplyResult, ReplyState
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.persona.test_jev_pipeline import Port

row = {'letter_id': 'jev-retry', 'content': '我醒了'}
server.store.letters[:] = [row]
server.store.personal_chats[:] = []
server.daily_life_runtime = None
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *a: None
class FailedWriter:
    async def run(self, request):
        assert row.get('companion_decision')
        return ReplyResult(request.request_id, ReplyState.FAILED, error_code='PROVIDER_TIMEOUT')
port = Port()
server.reply_pipeline = ReplyPipeline(FailedWriter(), reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
    discover_runtime_ports=False, companion_decision_port=port)
for _ in range(2):
    assert not asyncio.run(server.generate_reply(row['letter_id'], row['content']))
assert len(port.turns) == 1
assert row['letter_status'] == 'FAILED' and row['error_code'] == 'LLM_TIMEOUT'
''')


def test_unsupported_valid_plan_is_saved_and_not_reclassified_on_retry(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
import local_server as server
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.persona.test_jev_pipeline import Port, plan
from tests.persona.test_reply_semantic_wiring import Engine

row = {'letter_id': 'jev-two-assets', 'content': '分两条发来'}
server.store.letters[:] = [row]
server.store.personal_chats[:] = []
server.daily_life_runtime = None
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *a: None
value = plan()
value['proposal']['steps'].append(dict(id='s2', medium='text', parts=[dict(kind='text', content_ref='c1')],
    after=[dict(step_id='s1', event='delivered')], requirement_ids=[]))
engine, port = Engine('must not generate'), Port(value)
server.reply_pipeline = ReplyPipeline(engine, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
    discover_runtime_ports=False, companion_decision_port=port)
for _ in range(2):
    assert not asyncio.run(server.generate_reply(row['letter_id'], row['content']))
assert len(port.turns) == 1 and not engine.requests
assert row['error_code'] == 'JEV_PLAN_UNSUPPORTED'
assert len(row['companion_decision']['plan']['proposal']['steps']) == 2
''')


def test_jev_text_is_published_and_the_photo_planner_still_decides(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
import local_server as server
from runtime import image_reply
from original_client_letter_contract import serialize_letter_detail
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.persona.test_jev_pipeline import Port
from tests.persona.test_reply_semantic_wiring import Engine

row = {'letter_id': 'jev-text', 'content': '我醒了', 'image_reply_settings': {'enabled': True}}
server.store.letters[:] = [row]
server.store.personal_chats[:] = []
server.daily_life_runtime = None
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *a: None
server._commit_private_world_letter = lambda row: False
server.letters_adapter.remember_conversation = lambda *a: None
scheduled = []
image_reply.schedule = lambda *a: scheduled.append('photo')
server.reply_pipeline = ReplyPipeline(Engine('醒啦。'), reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
    discover_runtime_ports=False, companion_decision_port=Port())
assert asyncio.run(server.generate_reply(row['letter_id'], row['content']))
# A plain JEV text turn still lets the photo planner decide, as before 2.0. The
# written reply is kept while that decision runs, then shown with or without a photo.
assert row['letter_status'] == 'COMPLETED' and row['reply_text'] == '醒啦。'
assert 'image_status' not in row and scheduled == ['photo']
row['image_status'] = 'SKIPPED'
detail = serialize_letter_detail(row)
assert detail['letterStatus'] == 4 and detail.get('replyBody', detail['replyText']) == '醒啦。'
''')


def test_secondary_photo_only_on_plain_turns():
    from runtime.image_reply import secondary_photo_allowed
    plain = {'companion_decision': {'plan': {'understanding': {'requirements': []}}}, 'companion_delivery': 'text'}
    asked = {'companion_decision': {'plan': {'understanding': {'requirements': [{'id': 'r1'}]}}}, 'companion_delivery': 'audio_speech'}
    assert secondary_photo_allowed({}) and secondary_photo_allowed(plain)
    assert not secondary_photo_allowed(asked)

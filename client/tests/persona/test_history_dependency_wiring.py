"""Actual pipeline wire with persistent originals; models alone are deterministic fakes."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from runtime.memory.companion_memory_context import CompanionMemoryPromptBuilder, _ConversationMemoryView
from runtime.memory.source_retrieval import SourceRetrieval
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer


NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
USER = 'synthetic-owner'
OLD = '我已经收到那箱茶叶'
LATER = '刚才说错了，那箱茶叶还没有收到'


def fixture(tmp_path, *, oversized=False):
    index = SourceRetrieval(tmp_path / 'originals.sqlite3')
    index.put(USER, 'reply:old:1', OLD, '谢谢告诉我', NOW - timedelta(days=2))
    correction = LATER + ('很长的后续原文' * 4000 if oversized else '')
    index.put(USER, 'reply:correction:1', correction, '明白，是还没收到', NOW - timedelta(days=1))
    assert index.save_dependency(USER, 'reply:old:1', 'reply:correction:1',
                                 'user', 'user', OLD, LATER, 'correction')
    port = SimpleNamespace(_originals=index, enabled=True)
    builder = CompanionMemoryPromptBuilder(SimpleNamespace(enabled=False), port, user_id=USER)
    records = tuple(_ConversationMemoryView._convert(record)
                    for record in index.get_sources(USER, ('reply:old:1',)))
    from runtime.memory.recall import RecallResult
    history = builder.render(RecallResult(records), max_chars=20000).text
    assert OLD in history and LATER not in history
    wrapper = json.dumps({'untrusted': True, 'text': history}, ensure_ascii=False)
    messages = ({'role': 'system', 'content': '历史证据\n<untrusted_history>' + wrapper + '</untrusted_history>'},
                {'role': 'user', 'content': '那箱茶叶的情况呢？'})
    return index, builder, messages


def execute(builder, messages, mode, *, max_chars=20000, excluded=()):
    selection_calls, generation_calls = [], []

    class Gateway:
        async def complete_structured_scoped(self, request_messages, **kwargs):
            selection_calls.append((request_messages, kwargs))
            schema = kwargs['response_format']['schema']
            assert set(schema['required']) <= set(schema['properties']), 'strict schema must define every required field'
            packet = json.loads(request_messages[-1]['content'])
            selected = [item['id'] for item in packet['candidates']
                        if OLD in json.dumps(item, ensure_ascii=False)][:1]
            answer = {'selected_ids': selected}
            if 'dependencies' in kwargs['response_format']['schema']['properties']:
                answer['dependencies'] = []
            return SimpleNamespace(text=json.dumps(answer))

    async def generate(request):
        generation_calls.append(request.normalized_messages())
        return ReplyResult(request.request_id, ReplyState.COMPLETED, text='知道了')

    adapter = SimpleNamespace(gateway=Gateway(), memory_prompt_builder=builder,
                              _memory_source_exclusions=lambda: excluded)
    orchestrator = SimpleNamespace(run=generate, gateway=SimpleNamespace(adapter=adapter))
    pipeline = ReplyPipeline(orchestrator, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                             discover_runtime_ports=False)
    request = ReplyRequest(request_id='dependency-wire', messages=messages, max_input_chars=max_chars)
    result = asyncio.run(pipeline.run(request, ReplyContext.create(
        mode, trusted_time=TrustedTime(NOW), future_im_enabled=True)))
    assert result.state is ReplyState.COMPLETED
    assert len(generation_calls) == 1
    assert generation_calls[0][-1] == messages[-1]
    assert result.reviewer_calls == result.rewrite_calls == 0
    return generation_calls[0], selection_calls


@pytest.mark.parametrize('mode', [ReplyMode.TEXT_LETTER, ReplyMode.FUTURE_IM])
def test_selected_old_original_brings_persisted_correction_to_generation_wire(tmp_path, mode):
    index, builder, messages = fixture(tmp_path)
    wire, calls = execute(builder, messages, mode)
    text = json.dumps(wire, ensure_ascii=False)
    assert OLD in text and LATER in text
    assert 'reply:old:1' in text and 'reply:correction:1' in text
    assert len(calls) == 1
    # Merely preparing a reply never indexes the current, undelivered user turn.
    with index.connect() as db:
        assert db.execute('SELECT COUNT(DISTINCT source) FROM originals WHERE user=?', (USER,)).fetchone()[0] == 2


@pytest.mark.parametrize('mode', [ReplyMode.TEXT_LETTER, ReplyMode.FUTURE_IM])
@pytest.mark.parametrize('blocked', ['forgotten', 'capacity', 'excluded', 'future'])
def test_unavailable_correction_cannot_leave_isolated_old_original_on_wire(tmp_path, mode, blocked):
    index, builder, messages = fixture(tmp_path, oversized=blocked == 'capacity')
    excluded = ()
    if blocked == 'forgotten':
        index.forget(USER, 'reply:correction:1')
    elif blocked == 'excluded':
        excluded = ('reply:correction:1',)
    elif blocked == 'future':
        index.put(USER, 'reply:correction:1', LATER, '明白', NOW + timedelta(days=1))
    wire, _ = execute(builder, messages, mode, max_chars=7000 if blocked == 'capacity' else 20000,
                      excluded=excluded)
    text = json.dumps(wire, ensure_ascii=False)
    assert OLD not in text
    assert sum(len(message['content']) for message in wire) <= (7000 if blocked == 'capacity' else 20000)


def native_user(source, text, *, unconfirmed=False):
    metadata = {'source': source, 'event_id': source + ':user', 'actor': 'user',
                'evidence_kind': 'statement_only', 'time': NOW.isoformat(), 'channel': 'qq'}
    if unconfirmed:
        metadata['delivery_state'] = 'user_received_reply_unconfirmed'
    return {'role': 'user', 'content': '[历史消息 ' + json.dumps(metadata, ensure_ascii=False) + ']\n' + text}


@pytest.mark.parametrize('mode', [ReplyMode.TEXT_LETTER, ReplyMode.FUTURE_IM])
def test_unanswered_native_user_input_remains_temporary_without_original_index_entry(tmp_path, mode):
    index, builder, messages = fixture(tmp_path)
    pending = native_user('reply:pending:1', '刚才还有一个未回答的问题，请保留', unconfirmed=True)
    messages = (messages[0], pending, messages[-1])
    wire, _ = execute(builder, messages, mode)
    assert pending in wire
    with index.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM originals WHERE source=?', ('reply:pending:1',)).fetchone()[0] == 0


@pytest.mark.parametrize('mode', [ReplyMode.TEXT_LETTER, ReplyMode.FUTURE_IM])
def test_correction_link_in_another_user_namespace_cannot_supply_originals(tmp_path, mode):
    index, _, messages = fixture(tmp_path)
    builder = CompanionMemoryPromptBuilder(SimpleNamespace(enabled=False),
        SimpleNamespace(_originals=index, enabled=True), user_id='different-owner')
    wire, _ = execute(builder, messages, mode)
    text = json.dumps(wire, ensure_ascii=False)
    # This deliberately preassembled prompt already contains OLD. Dependency
    # lookup must not fetch the other owner's correction into this namespace.
    assert LATER not in text
    with index.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM source_dependencies WHERE user=?', ('different-owner',)).fetchone()[0] == 0


@pytest.mark.parametrize('provenance', [None, [], {'source_record_id': ['malformed']}])
def test_malformed_optional_source_metadata_cannot_abort_current_reply(tmp_path, provenance):
    _, builder, _ = fixture(tmp_path)
    from runtime.memory.history_selection import _block
    messages = ({'role': 'system', 'content': _block([[{
        'citation': 'broken', 'provenance': provenance, 'text': '损坏的可选历史',
        'speaker': 'user', 'evidence_scope': 'recorded_utterance'}]])},
        {'role': 'user', 'content': '只回答当前问题'})
    wire, _ = execute(builder, messages, ReplyMode.TEXT_LETTER)
    assert wire[-1] == messages[-1]

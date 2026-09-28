"""Corrections learned from this turn must survive beyond the recent window."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from runtime.memory.companion_memory_context import CompanionMemoryPromptBuilder
from runtime.memory.history_selection import _block, select_history_messages
from runtime.memory.source_retrieval import SourceRetrieval


NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)
OLD = '这是我自拍的照片'
NEW = '刚才说错了，那是网上找的图'


class Gateway:
    def __init__(self, selected, dependency):
        self.answer = {'selected_ids': selected, 'dependencies': [dependency] if dependency else []}
        self.calls = []

    async def complete_structured_scoped(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return SimpleNamespace(text=json.dumps(self.answer))


def setup(tmp_path):
    index = SourceRetrieval(tmp_path / 'originals.sqlite3')
    index.put('owner', 'reply:old:1', OLD, '', NOW - timedelta(days=1))
    builder = CompanionMemoryPromptBuilder(SimpleNamespace(enabled=False),
        SimpleNamespace(_originals=index, enabled=True), user_id='owner')
    return index, builder


def old_record():
    return {'citation': 'old:user', 'speaker': 'user',
            'provenance': {'source_record_id': 'reply:old:1'},
            'text': OLD, 'evidence_scope': 'recorded_utterance'}


def dependency(earlier='old:user', later='current'):
    return {'earlier': earlier, 'later': later, 'kind': 'correction',
            'earlier_quote': OLD, 'later_quote': NEW}


def execute(builder, gateway, current=NEW, *, sources=('reply:current:1',), native=(), optional=True):
    return asyncio.run(select_history_messages(
        ({'role': 'system', 'content': '角色' + (_block([[old_record()]]) if optional else '')},
         *native, {'role': 'user', 'content': current}), gateway,
        max_input_chars=20000, memory_builder=builder, as_of=NOW,
        current_source_ids=sources, current_user_text=current))


def native(source, text, *, pending=False):
    meta = {'source': source, 'event_id': source + ':user', 'time': NOW.isoformat()}
    if pending:
        meta['delivery_state'] = 'user_received_reply_unconfirmed'
    return {'role': 'user', 'content': '[历史消息 ' + json.dumps(meta) + ']\n' + text}


def test_current_received_correction_survives_failed_reply_and_restart(tmp_path):
    index, builder = setup(tmp_path)
    index.put_received('owner', 'received-user:new', NEW, NOW, exchange_source='reply:current:1')
    execute(builder, Gateway(['h0'], dependency()))
    assert index.dependencies('owner', ['reply:old:1']).relations
    # No assistant delivery or new search hit is needed after a process restart.
    reopened = SourceRetrieval(index.path)
    builder.conversation_memory._originals = reopened
    wire = execute(builder, Gateway(['h0'], None), '照片来源呢？', sources=())
    assert NEW in json.dumps(wire, ensure_ascii=False)
    with reopened.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM originals WHERE source='reply:current:1'").fetchone()[0] == 0


@pytest.mark.parametrize('pending', [False, True])
def test_native_only_correction_uses_one_existing_selection_pass(tmp_path, pending):
    index, builder = setup(tmp_path)
    if pending:
        index.put_received('owner', 'received-user:new', NEW, NOW, exchange_source='reply:new:1')
    else:
        index.put('owner', 'reply:new:1', NEW, '', NOW)
    gateway = Gateway([], dependency('reply:old:1:user', 'reply:new:1:user'))
    wire = execute(builder, gateway, '今天怎么样？', sources=(), optional=False,
        native=(native('reply:old:1', OLD), native('reply:new:1', NEW, pending=pending)))
    assert len(gateway.calls) == 1
    assert index.dependencies('owner', ['reply:old:1']).relations
    assert wire[-1]['content'] == '今天怎么样？'
    schema = gateway.calls[0][1]['response_format']['schema']['properties']['selected_ids']
    assert schema['maxItems'] == 0 and 'enum' not in schema['items']


@pytest.mark.parametrize('damage', ['owner', 'different_text', 'future', 'forgotten', 'assistant_only'])
def test_current_binding_requires_actual_owned_received_text_and_time(tmp_path, damage):
    index, builder = setup(tmp_path)
    owner = 'other' if damage == 'owner' else 'owner'
    text = '没有说过纠正' if damage == 'different_text' else NEW
    stamp = NOW + timedelta(days=1) if damage == 'future' else NOW
    if damage == 'assistant_only':
        index.put('owner', 'reply:current:1', '', NEW, NOW)
    else:
        index.put_received(owner, 'received-user:new', text, stamp, exchange_source='reply:current:1')
    if damage == 'forgotten':
        index.forget(owner, 'received-user:new')
    wire = execute(builder, Gateway(['h0'], dependency()))
    assert wire[-1]['content'] == NEW
    assert not index.dependencies('owner', ['reply:old:1']).relations


def test_merged_current_input_binds_quote_to_its_received_message(tmp_path):
    index, builder = setup(tmp_path)
    index.put_received('owner', 'received-user:new', NEW, NOW, exchange_source='reply:current:1')
    index.put_received('owner', 'received-user:other', '今天吃过午饭了', NOW,
                       exchange_source='reply:current:1')
    execute(builder, Gateway(['h0'], dependency()), NEW + '\n今天吃过午饭了')
    result = index.dependencies('owner', ['reply:old:1'])
    assert len(result.relations) == 1
    assert result.relations[0]['later_source'] == 'received-user:new'


def test_current_receipt_never_binds_a_quote_only_in_generated_observation(tmp_path):
    index, builder = setup(tmp_path)
    index.put_received('owner', 'received-user:new', '今天吃过午饭了', NOW,
                       exchange_source='reply:current:1')
    execute(builder, Gateway(['h0'], dependency()), NEW + '\n今天吃过午饭了')
    assert not index.dependencies('owner', ['reply:old:1']).relations


def test_native_old_statement_brings_unselected_new_correction_on_same_turn(tmp_path):
    index, builder = setup(tmp_path)
    index.put('owner', 'reply:new:1', NEW, '', NOW)
    newer = {**old_record(), 'citation': 'new:user',
             'provenance': {'source_record_id': 'reply:new:1'}, 'text': NEW}
    gateway = Gateway([], dependency('reply:old:1:user', 'new:user'))
    wire = asyncio.run(select_history_messages(
        ({'role': 'system', 'content': '角色' + _block([[newer]])},
         native('reply:old:1', OLD), {'role': 'user', 'content': '照片来源呢？'}),
        gateway, max_input_chars=20000, memory_builder=builder, as_of=NOW))
    assert NEW in json.dumps(wire, ensure_ascii=False)


def test_native_citation_matches_existing_expanded_original_when_adding_another_correction(tmp_path):
    index, builder = setup(tmp_path)
    prior = '照片是朋友替我拍的'
    index.put('owner', 'reply:prior:1', prior, '', NOW - timedelta(hours=1))
    assert index.save_dependency('owner', 'reply:old:1', 'reply:prior:1',
                                 'user', 'user', OLD, prior, 'correction')
    index.put('owner', 'reply:new:1', NEW, '', NOW)
    newer = {**old_record(), 'citation': 'new:user',
             'provenance': {'source_record_id': 'reply:new:1'}, 'text': NEW}
    gateway = Gateway([], dependency('reply:old:1:user', 'new:user'))
    wire = asyncio.run(select_history_messages(
        ({'role': 'system', 'content': '角色' + _block([[newer]])},
         native('reply:old:1', OLD), {'role': 'user', 'content': '照片来源呢？'}),
        gateway, max_input_chars=20000, memory_builder=builder, as_of=NOW))
    text = json.dumps(wire, ensure_ascii=False)
    assert OLD in text and prior in text and NEW in text


@pytest.mark.parametrize('mode_name', ['TEXT_LETTER', 'FUTURE_IM'])
def test_actual_reply_pipeline_supplies_current_receipt_identity(tmp_path, mode_name):
    from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
    from runtime.reply.reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
    from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
    from runtime.reply.reply_reviewer import NullReviewer

    index, builder = setup(tmp_path)
    index.put_received('owner', 'received-user:new', NEW, NOW, exchange_source='reply:current:1')
    gateway = Gateway(['h0'], dependency())
    wires = []

    async def generate(request):
        wires.append(request.normalized_messages())
        return ReplyResult(request.request_id, ReplyState.COMPLETED, text='知道了')

    adapter = SimpleNamespace(gateway=gateway, memory_prompt_builder=builder,
        _memory_source_exclusions=lambda: ('reply:current:1',))
    pipeline = ReplyPipeline(SimpleNamespace(run=generate, gateway=SimpleNamespace(adapter=adapter)),
        reviewer=NullReviewer(), rewriter=UnavailableRewriter(), discover_runtime_ports=False)
    request = ReplyRequest(messages=({'role': 'system', 'content': '角色' + _block([[old_record()]])},
        {'role': 'user', 'content': NEW}), max_input_chars=20000)
    result = asyncio.run(pipeline.run(request, ReplyContext.create(getattr(ReplyMode, mode_name),
        trusted_time=TrustedTime(NOW), future_im_enabled=True)))
    assert result.state is ReplyState.COMPLETED
    assert len(wires) == 1 and wires[0][-1]['content'] == NEW
    assert index.dependencies('owner', ['reply:old:1']).relations


def test_song_planner_cannot_revive_a_recalled_claim_without_its_correction(tmp_path, monkeypatch):
    from runtime.media import song_content
    from runtime.media.song_content import plan_song_content
    from tests.media.test_song_content_pipeline import RecordingGateway, _payload

    index, builder = setup(tmp_path)
    index.put_received('owner', 'received-user:new', NEW, NOW, exchange_source='reply:current:1')
    assert index.save_dependency('owner', 'reply:old:1', 'received-user:new',
                                 'user', 'user', OLD, NEW, 'correction')

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(song_content, 'datetime', Clock)

    class SongGateway(RecordingGateway):
        async def complete_structured_scoped(self, messages, **kwargs):
            return SimpleNamespace(text=json.dumps({'selected_ids': ['h0'], 'dependencies': []}))

    def messages(current, **kwargs):
        return ({'role': 'system', 'content': '角色' + _block([[old_record()]])},
                {'role': 'user', 'content': current})

    gateway = SongGateway(json.dumps(_payload()))
    adapter = SimpleNamespace(memory_prompt_builder=builder, reply_context_messages=messages,
                              _memory_source_exclusions=lambda: ())
    plan_song_content('写一首关于那张照片的歌', '好', 40, gateway=gateway, reply_adapter=adapter)
    assert len(gateway.calls) == 1
    assert NEW in json.dumps(gateway.calls[0][0], ensure_ascii=False)

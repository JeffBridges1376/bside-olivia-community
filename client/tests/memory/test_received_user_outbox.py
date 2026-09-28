import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from runtime.memory.conversation_memory_outbox import CanonicalMemoryOutbox


def chat(identifier='one', text='用户原话', **extra):
    return {'letter_id': identifier, 'channel': 'qq', 'binding_id': 'binding',
            'source_messages': {identifier: text}, 'content': text,
            'life_received_at': '2026-09-26T12:00:00+00:00',
            'delivery_status': 'RECEIVED', **extra}


def test_receipts_dedupe_merged_rows_without_promoting_drafts_or_batch_timestamps():
    from runtime.memory.received_user_originals import received_originals
    first = chat('one', '第一句')
    second = chat('two', '第二句', life_received_at='2026-09-26T12:02:00+00:00')
    merged = {**first, 'source_messages': {'one': '第一句', 'two': '第二句'},
              'content': '第一句\n第二句', 'reply_text': '未发出的助手草稿', 'delivery_status': 'GENERATING',
              'user_sent_at': '2020-01-01T00:00:00+00:00'}
    records = received_originals([merged, second, first, chat('proactive', origin='proactive')])
    assert len(records) == 2
    assert [record.user_message for record in records] == ['第一句', '第二句']
    assert records[1].occurred_at == datetime(2026, 9, 26, 12, 2, tzinfo=timezone.utc)
    assert set(records[1].exchange_sources) == {'reply:one:1', 'reply:two:1'}
    assert all('草稿' not in record.user_message for record in records)


def test_letters_and_binding_identity_are_separate_and_conflicts_are_not_indexed():
    from runtime.memory.received_user_originals import received_originals
    rows = [chat(), chat(binding_id='another'),
            {'letter_id': 'letter', 'content': '来信原话', 'life_received_at': '2026-09-26T12:00:00+00:00',
             'letter_status': 'PROCESSING', 'reply_text': '未交付回复'}]
    records = received_originals(rows)
    assert len({record.source_id for record in records}) == 3
    assert len(received_originals([chat(text='first'), chat(text='conflicting')])) == 0


def test_outbox_reads_durable_pending_users_but_does_not_commit_assistant(tmp_path):
    calls, committed = [], []
    class Memory:
        enabled = True
        def index_received_user(self, **kwargs):
            calls.append(kwargs)
            return True
    class Committer:
        memory = Memory()
        memory_lifecycle = None
        async def commit(self, delivery):
            committed.append(delivery)
            raise AssertionError('pending user input is not a delivered exchange')
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'letters': [], 'personal_chats': [chat(reply_text='draft')]}), encoding='utf-8')
    outbox = CanonicalMemoryOutbox(state, tmp_path / 'outbox.sqlite3', Committer(), user_id='owner')
    assert outbox._read_letters() == ()
    for _ in range(2):
        assert asyncio.run(outbox.scan_once()).status == 'available'
    assert len(calls) == 2  # Durable index owns idempotence, not a RAM-only seen set.
    assert calls[0] == calls[1]
    assert calls[0]['user_message'] == '用户原话'
    assert not committed


def test_paused_and_disabled_memory_prevent_receipt_writes():
    from runtime.memory.received_user_originals import index_received_rows
    class Memory:
        enabled = True
        def index_received_user(self, **kwargs):
            raise AssertionError('paused/disabled must not write')
    memory = Memory()
    assert index_received_rows(memory, [chat()], user_id='owner',
                               memory_lifecycle=SimpleNamespace(is_paused=lambda: True)) == ()
    memory.enabled = False
    assert index_received_rows(memory, [chat()], user_id='owner') == ()

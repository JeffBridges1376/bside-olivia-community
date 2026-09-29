"""Imported letters without a real date must reach the model as unknown time."""
from datetime import datetime, timedelta, timezone

from runtime.memory.conversation_memory_port import ConversationMemoryRecord
from runtime.memory.companion_memory_context import _ConversationMemoryView

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def record(source_id, occurred_at):
    return ConversationMemoryRecord(memory_id='m1', text='我在日本都没什么好吃的', user_id='local-user',
                                    source_id=source_id, occurred_at=occurred_at)


def test_ordering_stamp_of_imported_letter_is_unknown_time():
    converted = _ConversationMemoryView._convert(record('history:' + 'a' * 64, EPOCH + timedelta(seconds=3)))
    assert converted.occurred_at is None
    assert converted.provenance['occurred_at'] == ''


def test_real_dates_are_kept():
    dated = datetime(2026, 9, 12, 14, 38, tzinfo=timezone.utc)
    assert _ConversationMemoryView._convert(record('history:' + 'b' * 64, dated)).occurred_at == dated.isoformat()
    assert _ConversationMemoryView._convert(record('reply:x:1', dated)).occurred_at == dated.isoformat()


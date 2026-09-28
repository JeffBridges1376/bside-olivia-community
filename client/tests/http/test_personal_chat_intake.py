"""Durable reception is independent of generation and external delivery."""
import asyncio
import json

import pytest

from runtime.personal_chat.events import PersonalMessage, combine
from runtime.personal_chat.service import PersonalChatService


def message(key, text=None):
    return PersonalMessage('qq', '100', '200', key, text or key)


def test_next_input_is_durable_while_generation_is_running():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        rows, saved, sent = [], [], []
        def persist():
            saved[:] = json.loads(json.dumps(rows))
        async def generate(event, row):
            if event.message_id == 'first':
                entered.set()
                await release.wait()
            return event.text + ' reply'
        async def send(text):
            sent.append(text)
        async def commit(row):
            pass
        service = PersonalChatService(rows, persist, generate, commit, {'qq': ('100', '200')})
        first = asyncio.create_task(service.handle(message('first'), send))
        await entered.wait()
        try:
            await asyncio.wait_for(service.ingest(message('second')), .5)
            assert saved[-1]['content'] == 'second'
            assert saved[-1]['delivery_status'] == 'RECEIVED'
            assert saved[-1]['received_sequence'] > saved[0]['received_sequence']
            assert [event.text for event in service.pending('qq')] == ['second']
            assert sent == []
        finally:
            release.set()
            await first
        await service.handle(message('second'), send)
        assert sent == ['first\nsecond reply']
        assert service.pending('qq') == ()
    asyncio.run(scenario())


def test_separate_receipts_merge_once_and_regrouped_replay_does_not_resend():
    async def scenario():
        rows, generated, sent = [], [], []
        async def generate(event, row):
            generated.append(event.text)
            return 'answer'
        async def send(text):
            sent.append(text)
        async def commit(row):
            pass
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('100', '200')})
        a, b, c = message('a'), message('b'), message('c')
        await service.ingest(a)
        await service.ingest(b)
        revision = service.user_revision
        await service.ingest(a)
        assert service.user_revision == revision
        await service.handle(combine([a, b]), send)
        delivered = [r for r in rows if r['delivery_status'] == 'DELIVERED']
        assert len(delivered) == 1
        assert delivered[0]['source_messages'] == {'a': 'a', 'b': 'b'}
        assert len([r for r in rows if r.get('superseded_by') == delivered[0]['letter_id']]) == 1
        await service.handle(combine([b, c]), send)
        await service.handle(combine([a, b, c]), send)
        assert generated == ['a\nb', 'c']
        assert sent == ['answer', 'answer']
        assert service.pending('qq') == ()
        with pytest.raises(ValueError, match='ID_CONFLICT'):
            await service.ingest(message('a', 'changed'))
    asyncio.run(scenario())


def test_restart_recovers_only_received_inputs_for_the_same_binding():
    async def scenario():
        async def unexpected(*args):
            raise AssertionError('reception must not call generation or delivery')
        service = PersonalChatService([], lambda: None, unexpected, unexpected, {'qq': ('100', '200')})
        for key in ('received', 'sending', 'unknown', 'generated', 'other-owner'):
            await service.ingest(message(key))
        for row, status in zip(service.rows[1:4], ('SENDING', 'DELIVERY_UNCONFIRMED', 'GENERATED')):
            row['delivery_status'] = status
        service.rows[-1]['binding_id'] = 'different-binding'
        restarted = PersonalChatService(json.loads(json.dumps(service.rows)), lambda: None,
                                        unexpected, unexpected, service.bindings)
        assert [e.message_id for e in restarted.pending('qq')] == ['received']
        assert restarted.pending('wechat') == ()
        await restarted.ingest(message('after-restart'))
        assert restarted.rows[-1]['received_sequence'] > service.rows[-1]['received_sequence']
        with pytest.raises(ValueError, match='OWNER_MISMATCH'):
            await restarted.ingest(PersonalMessage('qq', '100', 'foreign', 'x', 'hello'))
    asyncio.run(scenario())


def test_persist_failure_prevents_generation_and_is_retried_before_processing():
    async def scenario():
        rows, generated = [], []
        failing = True
        def persist():
            if failing:
                raise OSError('synthetic disk unavailable')
        async def generate(event, row):
            generated.append(event.text)
            return 'answer'
        async def noop(*args):
            pass
        service = PersonalChatService(rows, persist, generate, noop, {'qq': ('100', '200')})
        with pytest.raises(OSError):
            await service.handle(message('one'), noop)
        assert generated == []
        failing = False
        await service.handle(message('one'), noop)
        assert generated == ['one']
        assert len(rows) == 1
    asyncio.run(scenario())


def test_merged_receipt_after_a_cross_channel_reply_is_not_a_stale_turn():
    async def scenario():
        rows, generated = [], []
        async def generate(event, row):
            generated.append(event.text)
            return 'answer'
        async def noop(*args):
            pass
        service = PersonalChatService(rows, lambda: None, generate, noop,
            {'qq': ('100', '200'), 'wechat': ('bot', '200')})
        first, last = message('one'), message('three')
        await service.ingest(first)
        await service.handle(PersonalMessage('wechat', 'bot', '200', 'two', 'middle'), noop)
        await service.ingest(last)
        await service.handle(combine([first, last]), noop)
        assert generated == ['middle', 'one\nthree']
        assert rows[0]['delivery_status'] == 'DELIVERED'
    asyncio.run(scenario())

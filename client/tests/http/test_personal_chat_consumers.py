import asyncio

from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService


def test_slow_consumer_does_not_block_next_ordered_delivery():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        delivered, consumed, rows = [], [], []
        async def generate(event, row):
            return event.text
        async def send(text):
            delivered.append(text)
            return 'ack'
        async def consume(row):
            consumed.append(row['letter_id'])
            entered.set()
            await release.wait()
        service = PersonalChatService(rows, lambda: None, generate, consume, {'qq': ('a', 'u')})
        first = PersonalMessage('qq', 'a', 'u', '1', 'first')
        await service.handle(first, send)
        await entered.wait()
        await asyncio.wait_for(service.handle(PersonalMessage('qq', 'a', 'u', '2', 'second'), send), .2)
        await service.handle(first, send)
        await service.recover()
        await asyncio.sleep(0)
        assert delivered == ['first', 'second']
        assert len(consumed) == len(set(consumed)) == 2
        assert not service.lock.locked()
        assert len(service.consumer_tasks) == 2
        release.set()
        await asyncio.gather(*service.consumer_tasks.values())
        await asyncio.sleep(0)
        assert not service.consumer_tasks
    asyncio.run(asyncio.wait_for(run(), 2))


def test_consumer_shutdown_cancellation_keeps_delivery_and_can_recover():
    async def run():
        entered = asyncio.Event()
        async def consume(row):
            entered.set()
            await asyncio.Future()
        row = {'letter_id': 'x', 'delivery_status': 'DELIVERED', 'daily_life_status': 'PENDING'}
        service = PersonalChatService([row], lambda: None, None, consume, {})
        await service.recover()
        await entered.wait()
        tasks = list(service.consumer_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
        assert not service.consumer_tasks
        assert row['delivery_status'] == 'DELIVERED'
        assert row['daily_life_status'] == 'PENDING'
        async def completed(row):
            row['daily_life_status'] = 'COMMITTED'
        service.commit = completed
        await service.recover()
        await asyncio.gather(*service.consumer_tasks.values())
        assert row['daily_life_status'] == 'COMMITTED'
    asyncio.run(asyncio.wait_for(run(), 2))


def test_consumer_failure_remains_observable_without_resending():
    async def run():
        rows, sent, attempts = [], [], []
        async def generate(event, row):
            return 'reply'
        async def send(text):
            sent.append(text)
            return 'ack'
        async def consume(row):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError('unexpected storage failure')
            row['daily_life_status'] = 'COMMITTED'
        service = PersonalChatService(rows, lambda: None, generate, consume, {'qq': ('a', 'u')})
        event = PersonalMessage('qq', 'a', 'u', '1', 'hello')
        await service.handle(event, send)
        assert rows[0]['delivery_status'] == 'DELIVERED'
        assert rows[0]['consumer_error_code'] == 'PERSONAL_CHAT_CONSUMER_UNAVAILABLE'
        await service.handle(event, send)
        await asyncio.gather(*service.consumer_tasks.values())
        assert sent == ['reply']
        assert len(attempts) == 2
        assert rows[0]['daily_life_status'] == 'COMMITTED'
    asyncio.run(asyncio.wait_for(run(), 2))

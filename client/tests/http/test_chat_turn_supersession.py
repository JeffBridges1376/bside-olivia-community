"""Input revisions invalidate unsent drafts, never confirmed delivery history."""
import asyncio
from dataclasses import replace

import pytest

from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService


def incoming(identifier, text):
    return PersonalMessage('qq', '100', '200', identifier, text)


async def finish_pending(service, send):
    for _ in range(4):
        pending = service.pending('qq')
        if not pending:
            break
        await service.handle(pending[0], send)
    assert not service.pending('qq'), 'finite input burst must eventually drain'
    tasks = tuple(service.consumer_tasks.values()) + tuple(service.photo_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)


def test_new_correction_keeps_unanswered_question_and_discards_old_draft():
    async def scenario():
        rows, generated, sent, committed = [], [], [], []
        first = incoming('1', '午饭吃什么？我准备睡觉了')
        correction = incoming('2', '不睡了，我醒着，继续说午饭')

        async def generate(event, row):
            generated.append(dict(event.sources))
            if len(generated) == 1:
                await service.ingest(correction)
                return '旧稿：那你睡吧'
            assert dict(event.sources) == dict(first.sources + correction.sources)
            return '新稿：醒着就继续聊午饭'

        async def send(text):
            sent.append(text)
            return 'ack'

        async def commit(row):
            committed.append(row['reply_text'])

        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('100', '200')})
        await service.handle(first, send)
        await finish_pending(service, send)
        assert sent == ['新稿：醒着就继续聊午饭']
        assert committed == sent
        delivered = [row for row in rows if row['delivery_status'] == 'DELIVERED']
        assert len(delivered) == 1
        assert delivered[0]['source_messages'] == dict(first.sources + correction.sources)
    asyncio.run(scenario())


def test_new_input_during_audio_upload_cannot_send_old_audio_or_launch_old_photo():
    async def scenario():
        rows, generated, sent, photos = [], [], [], []
        first, second = incoming('1', '说声晚安'), incoming('2', '不睡了，说早安吧')

        async def generate(event, row):
            generated.append(dict(event.sources))
            row['prepared_audio'] = 'old.wav' if len(generated) == 1 else 'new.wav'
            row['image_status'] = 'PENDING'
            return '晚安旧稿' if len(generated) == 1 else '早安新稿'

        class Sender:
            async def __call__(self, text):
                sent.append(text)
                return 'text-ack'

            async def prepare_audio(self, path):
                if path == 'old.wav':
                    await service.ingest(second)
                return path

            async def audio(self, path):
                sent.append(path)
                return 'audio-ack'

            async def image(self, path):
                return 'image-ack'

        async def commit(row):
            pass

        async def photo(row, send):
            photos.append((row['reply_text'], dict(row['source_messages'])))
            row['image_status'] = 'SKIPPED'

        service = PersonalChatService(rows, lambda: None, generate, commit,
                                      {'qq': ('100', '200')}, photo=photo)
        sender = Sender()
        await service.handle(first, sender)
        await finish_pending(service, sender)
        assert sent == ['new.wav']
        assert photos == [('早安新稿', dict(first.sources + second.sources))]
    asyncio.run(scenario())


def test_new_input_after_sending_reservation_does_not_retract_first_delivery():
    async def scenario():
        rows, sent = [], []
        first, second = incoming('1', '第一问'), incoming('2', '第二问')

        async def generate(event, row):
            return '回答' + event.message_id

        async def send(text):
            assert any(row['delivery_status'] == 'SENDING' for row in rows)
            sent.append(text)
            if len(sent) == 1:
                await service.ingest(second)
            return 'ack-' + str(len(sent))

        async def commit(row):
            pass

        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('100', '200')})
        await service.handle(first, send)
        await finish_pending(service, send)
        assert sent == ['回答1', '回答2']
        assert [row['delivery_status'] for row in rows] == ['DELIVERED', 'DELIVERED']
    asyncio.run(scenario())


def test_identical_platform_replay_during_generation_does_not_invalidate_draft():
    async def scenario():
        rows, generated, sent = [], [], []
        first = incoming('1', '同一条原始消息')

        async def generate(event, row):
            generated.append(event)
            await service.ingest(first)
            return '正常回复'

        async def send(text):
            sent.append(text)
            return 'ack'

        async def commit(row):
            pass

        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('100', '200')})
        await service.handle(first, send)
        await finish_pending(service, send)
        assert len(generated) == 1
        assert sent == ['正常回复']
    asyncio.run(scenario())


def test_continuous_updates_yield_after_two_generations_and_finite_burst_eventually_answers():
    async def scenario():
        rows, generated, sent = [], [], []
        inputs = [incoming(str(i), '未答问题' + str(i)) for i in (1, 2, 3)]

        async def generate(event, row):
            generated.append(dict(event.sources))
            if len(generated) <= 2:
                await service.ingest(inputs[len(generated)])
                return '失效草稿' + str(len(generated))
            return '合并回答全部问题'

        async def send(text):
            sent.append(text)
            return 'ack'

        async def commit(row):
            pass

        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('100', '200')})
        await service.handle(inputs[0], send)
        assert len(generated) == 2, 'one handling pass has a bounded regeneration budget'
        assert not sent
        pending_sources = {key: value for event in service.pending('qq') for key, value in event.sources}
        assert pending_sources == {event.message_id: event.text for event in inputs}
        await asyncio.sleep(0)
        assert len(generated) == 2, 'yielding must not launch an unowned regeneration loop'
        await finish_pending(service, send)
        assert generated[-1] == pending_sources
        assert sent == ['合并回答全部问题']
        assert len(generated) == 3
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['late_platform_time', 'other_binding'])
def test_unrelated_or_late_receipt_does_not_invalidate_current_draft(kind):
    async def scenario():
        rows, generated, sent = [], [], []
        first = replace(incoming('1', '现在的问题'), sent_at='2026-09-26T10:00:00+00:00')
        second = (replace(incoming('2', '迟到的昨天消息'), sent_at='2026-09-25T10:00:00+00:00')
                  if kind == 'late_platform_time' else
                  PersonalMessage('wechat', '300', '200', '2', '微信补充'))

        async def generate(event, row):
            generated.append(dict(event.sources))
            await service.ingest(second)
            return '当前回答'

        async def send(text):
            sent.append(text)
            return 'ack'

        async def commit(row):
            pass

        service = PersonalChatService(rows, lambda: None, generate, commit,
                                      {'qq': ('100', '200'), 'wechat': ('300', '200')})
        await service.handle(first, send)
        assert generated == [{'1': '现在的问题'}]
        assert sent == ['当前回答']
        assert rows[0]['source_messages'] == {'1': '现在的问题'}
        if kind == 'late_platform_time':
            await finish_pending(service, send)
            assert rows[1]['delivery_status'] == 'SKIPPED'
            assert len(generated) == 1
        else:
            assert [event.message_id for event in service.pending('wechat')] == ['2']
    asyncio.run(scenario())


def test_sending_reservation_persist_excludes_concurrent_intake_but_not_network_send():
    async def scenario():
        rows, sent, intake_tasks = [], [], []
        first, second = incoming('1', '第一问'), incoming('2', '第二问')

        async def persist():
            if rows and rows[0]['delivery_status'] == 'SENDING' and not intake_tasks:
                task = asyncio.create_task(service.ingest(second))
                intake_tasks.append(task)
                await asyncio.sleep(0)
                assert not task.done(), 'input admission must wait for durable SENDING reservation'
                assert len(rows) == 1

        async def generate(event, row):
            return '回答' + event.message_id

        async def send(text):
            # Network ACK must not hold the intake lock after the reservation.
            await asyncio.wait_for(intake_tasks[0], .5)
            assert len(rows) == 2
            sent.append(text)
            return 'ack'

        async def commit(row):
            pass

        service = PersonalChatService(rows, persist, generate, commit, {'qq': ('100', '200')})
        await service.handle(first, send)
        await finish_pending(service, send)
        assert sent == ['回答1', '回答2']
    asyncio.run(scenario())

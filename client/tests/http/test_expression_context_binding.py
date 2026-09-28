import asyncio
from datetime import datetime, timezone
import hashlib
import json

import pytest

from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService


def context():
    from runtime.reply.character_emotion_context import freeze_expression_context
    return freeze_expression_context('synthetic-request', datetime(2026, 9, 27, tzinfo=timezone.utc),
                                     world=None, emotion=None)


@pytest.mark.parametrize('delivery', ['text', 'audio', 'audio_fallback'])
def test_service_binds_final_displayed_or_spoken_text_and_preserves_on_restart(delivery):
    from runtime.reply.character_emotion_context import store_expression_context, checked_expression_context
    async def scenario():
        rows, sent, snapshots = [], [], []
        async def generate(event, row):
            text = '醒了。先喝点水。'
            store_expression_context(row, context(), text)
            if delivery != 'text':
                row['prepared_audio'] = 'synthetic.wav'
            return text
        def persist():
            snapshots.append(json.loads(json.dumps(rows)))
        async def commit(row):
            pass
        class Sender:
            async def __call__(self, text):
                sent.append(text)
            async def prepare_audio(self, path):
                if delivery == 'audio_fallback':
                    raise RuntimeError('synthetic upload failed')
                return path
            async def audio(self, path):
                sent.append('audio:' + path)
        service = PersonalChatService(rows, persist, generate, commit, {'qq': ('bot', 'owner')})
        event = PersonalMessage('qq', 'bot', 'owner', '1', '我醒了')
        await service.handle(event, Sender())
        expected = '醒了。先喝点水。' if delivery == 'audio' else '醒了\n先喝点水'
        row = rows[0]
        assert row['reply_text'] == expected
        bound = checked_expression_context(row)
        assert bound and bound['binding']['reply_sha256'] == hashlib.sha256(expected.encode()).hexdigest()
        assert bound['binding']['reply_revision'] == row['reply_revision'] == 1
        assert bound['binding']['generation_attempts'] == row['generation_attempts']
        sending = next(batch[0] for batch in snapshots if batch[0]['delivery_status'] == 'SENDING')
        assert checked_expression_context(sending)
        restored = json.loads(json.dumps(row))
        assert checked_expression_context(restored) == bound
        restored['reply_text'] += '另一个草稿'
        assert checked_expression_context(restored) is None
    asyncio.run(scenario())


@pytest.mark.parametrize('status', ['unknown', 'unconfirmed'])
def test_unknown_delivery_keeps_binding_without_generating_again(status):
    from runtime.reply.character_emotion_context import store_expression_context, checked_expression_context
    async def scenario():
        rows, calls = [], []
        async def generate(event, row):
            calls.append('generate')
            store_expression_context(row, context(), '先歇会儿。')
            return '先歇会儿。'
        async def send(text):
            calls.append('send')
            if status == 'unknown':
                raise TimeoutError()
            return {}
        if status == 'unconfirmed':
            send.delivery_confirmation = lambda _: 'UNCONFIRMED'
        async def commit(row):
            calls.append('commit')
        event = PersonalMessage('qq', 'bot', 'owner', '1', '累了')
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('bot', 'owner')})
        if status == 'unknown':
            with pytest.raises(TimeoutError):
                await service.handle(event, send)
        else:
            await service.handle(event, send)
        before = checked_expression_context(rows[0])
        assert before is not None
        restarted = PersonalChatService(json.loads(json.dumps(rows)), lambda: None, generate, commit, service.bindings)
        await restarted.recover()
        assert checked_expression_context(restarted.rows[0]) == before and calls == ['generate', 'send']
    asyncio.run(scenario())


def test_new_input_and_old_pipeline_result_cannot_reuse_expression_context():
    from runtime.personal_chat.service import _clear_draft
    from runtime.reply.character_emotion_context import store_expression_context, checked_expression_context
    row = {'reply_text': '旧回复', 'input_revision': 1, 'generation_attempts': 1}
    store_expression_context(row, context(), row['reply_text'])
    assert checked_expression_context(row)
    row['input_revision'] += 1
    assert checked_expression_context(row) is None
    _clear_draft(row)
    assert 'expression_context' not in row
    store_expression_context(row, context(), '新回复')
    store_expression_context(row, None, '旧接口回复')
    assert 'expression_context' not in row


def test_failed_generation_retry_clears_previous_metadata_even_for_legacy_generator():
    from runtime.reply.character_emotion_context import store_expression_context
    async def scenario():
        rows = []
        async def generate(event, row):
            if row['generation_attempts'] == 1:
                store_expression_context(row, context(), '未完成草稿')
                raise RuntimeError('PERSONAL_CHAT_GENERATION_FAILED')
            assert 'expression_context' not in row
            return '旧版生成器正常回复。'
        async def send(text):
            pass
        service = PersonalChatService(rows, lambda: None, generate, send, {'qq': ('bot', 'owner')})
        event = PersonalMessage('qq', 'bot', 'owner', '1', '你好')
        with pytest.raises(RuntimeError):
            await service.handle(event, send)
        await service.handle(event, send)
        assert rows[0]['delivery_status'] == 'DELIVERED' and 'expression_context' not in rows[0]
    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ['hash', 'time', 'oversize', 'not_json', 'nan', 'invalid_unicode'])
def test_optional_metadata_damage_is_neutral_without_breaking_reply(damage):
    from runtime.reply.character_emotion_context import store_expression_context, checked_expression_context
    snapshot = context()
    if damage == 'hash':
        snapshot['view_sha256'] = 'wrong'
    elif damage == 'time':
        snapshot['as_of'] = 'not a timestamp'
    elif damage == 'oversize':
        snapshot['world'] = {'note': 'x' * 21000}
    elif damage == 'not_json':
        snapshot['world'] = {'note': object()}
    elif damage == 'nan':
        snapshot['world'] = {'value': float('nan')}
    else:
        snapshot['world'] = {'note': '\ud800'}
    row = {'reply_text': '正常回复', 'expression_context': context()}
    store_expression_context(row, snapshot, row['reply_text'])
    assert row == {'reply_text': '正常回复'}
    assert checked_expression_context({'reply_text': '旧版正常回复'}) is None


@pytest.mark.parametrize('changed', ['input_revision', 'generation_attempts', 'reply_revision'])
def test_normalization_cannot_resurrect_a_stale_binding(changed):
    from runtime.reply.character_emotion_context import store_expression_context, rebind_expression_context
    row = {'reply_text': '旧回复。', 'input_revision': 1, 'generation_attempts': 1, 'reply_revision': 1}
    store_expression_context(row, context(), row['reply_text'])
    row[changed] = 2
    row['reply_text'] = '旧回复'
    rebind_expression_context(row, previous_text='旧回复。')
    assert 'expression_context' not in row

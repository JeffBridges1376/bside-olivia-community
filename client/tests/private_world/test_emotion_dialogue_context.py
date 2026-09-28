import asyncio
from datetime import timedelta

from tests.private_world.test_character_emotion_runtime import NOW, Gateway, receipt
from runtime.private_world.character_emotion_runtime import CharacterEmotionRuntime
from runtime.private_world.daily_life import DailyLifeStore


def test_emotion_reads_only_preceding_delivered_dialogue(tmp_path):
    def row(key, minutes, **changes):
        return dict(letter_id=key, channel='qq', delivery_status='DELIVERED',
                    private_world_occurred_at=(NOW + timedelta(minutes=minutes)).isoformat(),
                    content='再陪我一会儿', reply_text='今天真的要睡了。', **changes)
    rows = [row('past', -2), row('future', 1), row('current', 0)]
    pending = row('draft', -1)
    pending['delivery_status'] = 'GENERATED'
    rows.append(pending)
    gateway = Gateway()
    runtime = CharacterEmotionRuntime(DailyLifeStore(tmp_path / 'life.sqlite3'),
        lambda: gateway, lambda: '大学生', dialogue_rows=lambda: rows)
    asyncio.run(runtime.evaluate_received([receipt()], now=NOW))
    context = gateway.calls[0][0]['assessment']['source_contexts'][receipt().source_id]
    assert [r['source_id'] for r in context['recent_dialogue']] == ['reply:past:1']
    assert context['recent_dialogue'][0]['character_reply'] == '今天真的要睡了。'
    assert runtime.view(NOW)['reactions']


def test_delayed_letters_and_uncommitted_replies_are_not_past_speech(tmp_path):
    base = dict(letter_id='letter', letter_status='COMPLETED', private_world_status='COMMITTED',
                private_world_occurred_at=(NOW-timedelta(minutes=2)).isoformat(),
                content='再说一句', reply_text='晚安。')
    rows = [base, dict(base, letter_id='delayed', reply_not_before=(NOW+timedelta(minutes=1)).timestamp()),
            dict(base, letter_id='pending', private_world_status='PENDING'),
            dict(base, letter_id='old', private_world_occurred_at=(NOW-timedelta(days=2)).isoformat())]
    from runtime.private_world.daily_life_runtime import DailyLifeRuntime
    life = DailyLifeRuntime(DailyLifeStore(tmp_path/'life.sqlite3'), Gateway, lambda:'大学生',
                           dialogue_rows=lambda:rows)
    assert [r['source_id'] for r in life.emotion._dialogue_at(NOW)] == ['reply:letter:1']


def test_context_change_during_evaluation_retries_before_commit(tmp_path):
    rows = [dict(letter_id='past', channel='qq', delivery_status='DELIVERED',
                 private_world_occurred_at=(NOW-timedelta(minutes=1)).isoformat(),
                 content='还没睡吗', reply_text='该睡了。')]
    from tests.private_world.test_character_emotion_runtime import output
    def evaluate(packet):
        result = output(packet)
        rows[0]['reply_text'] = '我真的要休息了。'
        return result
    gateway = Gateway(evaluate)
    runtime = CharacterEmotionRuntime(DailyLifeStore(tmp_path/'life.sqlite3'), lambda:gateway,
        lambda:'大学生', dialogue_rows=lambda:rows)
    asyncio.run(runtime.evaluate_received([receipt()], now=NOW))
    assert len(gateway.calls) == 2
    assert runtime.error_code is None

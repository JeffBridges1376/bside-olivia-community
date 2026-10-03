"""Single-character topics recall existing evidence without widening disclosure."""
import json
from datetime import datetime, timezone

import pytest

from runtime.private_world.daily_life import DailyLifeStore


NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def write(store, identifier, title, quote, status, *, kind='linli'):
    store.record_exchange('reply:' + identifier, '好。', quote, [dict(
        id=identifier, title=title, detail=quote, quote=quote, status=status,
        kind=kind, actor='linli')], occurred_at=NOW)


def test_mixed_query_recalls_planned_single_character_topic_after_restart(tmp_path):
    path = tmp_path / 'life.sqlite3'
    store = DailyLifeStore(path)
    write(store, 'porridge', '我打算明早熬粥，', '我打算明早熬粥，', 'planned')
    write(store, 'laundry', '今天的衣服已经晾好了。', '今天的衣服已经晾好了。', 'completed')
    restarted = DailyLifeStore(path)
    before = restarted.snapshot(NOW)
    encoded = restarted.reply_context('衣服晾好了吗，粥是已经熬好还是明天再熬？', now=NOW)
    rows = {item['id']: item for item in json.loads(encoded)['threads']}
    assert set(rows) == {'porridge', 'laundry'}
    assert rows['porridge']['status'] == 'planned'
    assert rows['laundry']['status'] == 'completed'
    assert rows['porridge']['quote'] == '我打算明早熬粥，'
    assert len(encoded) <= 1800
    assert restarted.snapshot(NOW) == before


@pytest.mark.parametrize('title,quote,status,query,kind', [
    ('寄书', '寄书这件事取消了。', 'cancelled', '书现在还会寄吗？', 'shared'),
    ('我暂停修伞', '我暂停修伞，过几天再修。', 'paused', '伞是否修好？', 'linli'),
])
def test_single_character_topic_transfers_without_task_vocabulary(tmp_path, title, quote, status, query, kind):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    write(store, 'topic', title, quote, status, kind=kind)
    item, = json.loads(store.reply_context(query, now=NOW))['threads']
    assert item['id'] == 'topic' and item['status'] == status
    assert (item['actor'], item['quote'], item['source_id']) == ('linli', quote, 'reply:topic')


@pytest.mark.parametrize('query', ['我呢？你呢？现在怎么样？', '今天就先这样吧。'])
def test_conversational_stop_characters_do_not_recall_all_threads(tmp_path, query):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    for identifier, title, quote in [
        ('book', '我打算明天读书', '我打算明天读书。'),
        ('walk', '今天我先去散步', '今天我先去散步。'),
        ('plant', '我想下周给植物换土', '我想下周给植物换土。'),
    ]:
        write(store, identifier, title, quote, 'planned')
    assert json.loads(store.reply_context(query, now=NOW))['threads'] == []


def test_single_character_fallback_does_not_use_another_action_in_full_quote(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    write(store, 'buttons', '补纽扣', '纽扣已经补好了，朋友明天会熬粥。', 'completed')
    assert json.loads(store.reply_context('粥熬了吗？', now=NOW))['threads'] == []


def test_character_waiting_requires_current_topic_and_keeps_speaker_attribution(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    quote = '你选一本书，有空我翻翻。'
    write(store, 'book', '选一本书', quote, 'awaiting_user', kind='shared')
    assert json.loads(store.reply_context('晚安。', related_text='书呢？', now=NOW))['threads'] == []
    item, = json.loads(store.reply_context('书呢？', now=NOW))['threads']
    assert item['status'] == 'linli_waiting' and item['actor'] == 'linli'
    assert item['commitment_evidence'] == 'requires_user_statement'
    assert store.snapshot(NOW)['shared'][0]['status'] == 'awaiting_user'


def test_single_character_recall_retains_two_item_and_character_limits(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    for identifier, title in [('post', '寄书'), ('read', '读书'), ('repair', '修书')]:
        write(store, identifier, title, '我准备' + title + '。', 'planned')
    encoded = store.reply_context('书呢？', now=NOW)
    assert len(json.loads(encoded)['threads']) == 2
    assert len(encoded) <= 1800
    assert len(store.reply_context('书呢？', now=NOW, max_chars=400)) <= 400


def test_hypothetical_query_does_not_turn_recalled_plan_into_completion(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    write(store, 'porridge', '熬粥', '我准备明早熬粥。', 'planned')
    before = store.snapshot(NOW)
    item, = json.loads(store.reply_context('假如粥明天已经熬好了呢？', now=NOW))['threads']
    assert item['status'] == 'planned'
    assert item['evidence_kind'] == 'character_statement'
    assert store.snapshot(NOW) == before

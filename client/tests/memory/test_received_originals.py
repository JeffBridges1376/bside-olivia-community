from datetime import datetime, timezone

from runtime.memory.source_retrieval import SourceRetrieval

NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


def test_receipt_has_no_assistant_and_conflicts_do_not_overwrite(tmp_path):
    index = SourceRetrieval(tmp_path / 'index.db')
    assert index.put_received('u', 'received-user:a', '这是网图', NOW)
    assert index.put_received('u', 'received-user:a', '这是网图', NOW)
    assert not index.put_received('u', 'received-user:a', '修改过的原话', NOW)
    assert not index.put_received('u', 'reply:a', '这是网图', NOW)
    records = index.get_sources('u', ['received-user:a'])
    assert len(records) == 1 and records[0].metadata['speaker'] == 'user'
    assert records[0].text == '这是网图'


def test_alias_forget_propagates_both_directions_and_future_reply(tmp_path):
    index = SourceRetrieval(tmp_path / 'index.db')
    assert index.put_received('u', 'received-user:a', '这是网图', NOW, exchange_source='reply:a')
    assert index.put_received('u', 'received-user:b', '不是自拍', NOW, exchange_source='reply:a')
    index.forget('u', 'received-user:a')
    assert index.forgotten_sources('u') == {'received-user:a', 'received-user:b', 'reply:a'}
    index.put('u', 'reply:a', '这是网图\n不是自拍', '明白了', NOW)
    assert not index.get_sources('u', ['reply:a', 'received-user:b'])
    assert not index.put_received('u', 'received-user:a', '这是网图', NOW)
    assert index.put_received('other', 'received-user:a', '另一个用户', NOW)


def test_late_alias_to_forgotten_receipt_prevents_resurrection(tmp_path):
    index = SourceRetrieval(tmp_path / 'index.db')
    index.put_received('u', 'received-user:a', '这是网图', NOW)
    index.forget('u', 'received-user:a')
    index.put('u', 'reply:a', '这是网图', '明白了', NOW)
    assert index.alias_received('u', 'reply:a', ['received-user:a'])
    assert not index.get_sources('u', ['reply:a'])
    index.put('u', 'reply:a', '这是网图', '明白了', NOW)
    assert not index.get_sources('u', ['reply:a'])


def test_canonical_records_expose_receipt_lineage_and_dependencies(tmp_path):
    index = SourceRetrieval(tmp_path / 'index.db')
    index.put_received('u', 'received-user:old', '这是自拍', NOW)
    index.put_received('u', 'received-user:new', '这是网图', NOW, exchange_source='reply:new')
    index.put('u', 'reply:new', '这是网图', '知道了', NOW)
    assert index.save_dependency('u', 'received-user:old', 'received-user:new', 'user', 'user', '这是自拍', '这是网图', 'correction')
    records = index.get_sources('u', ['reply:new'])
    assert all('received-user:new' in r.metadata['source_aliases'] for r in records)
    assert all('source_aliases' in r.metadata for r in index.search('这是网图', 'u') if r.source_id in {'received-user:new', 'reply:new'})
    assert index.dependencies('u', ['received-user:old']).relations
    index.forget('u', 'reply:new')
    assert index.dependencies('u', ['received-user:old']).blocked_source_ids == ('received-user:old',)


def test_forget_reply_before_receipt_arrives_and_invalid_timestamp(tmp_path):
    index = SourceRetrieval(tmp_path / 'index.db')
    index.forget('u', 'reply:future')
    assert not index.put_received('u', 'received-user:future', '这是网图', NOW, exchange_source='reply:future')
    assert 'received-user:future' in index.forgotten_sources('u')
    assert not index.put_received('u', 'received-user:undated', '这是网图', None)
    assert not index.put_received('u', 'received-user:naive', '这是网图', NOW.replace(tzinfo=None))


def test_canonical_hit_follows_receipt_correction_without_unwritten_reply(tmp_path):
    index = SourceRetrieval(tmp_path / 'index.db')
    index.put_received('u', 'received-user:a', '这是自拍', NOW, exchange_source='reply:a')
    index.put('u', 'reply:a', '这是自拍', '原来的回复', NOW)
    index.put_received('u', 'received-user:b', '这是网图', NOW, exchange_source='reply:b')
    assert index.save_dependency('u', 'received-user:a', 'received-user:b', 'user', 'user', '这是自拍', '这是网图', 'correction')
    result = index.dependencies('u', ['reply:a'], before=NOW)
    assert not result.blocked_source_ids
    assert {r.source_id for r in result.records} == {'reply:a', 'received-user:a', 'received-user:b'}
    assert all(r.metadata['speaker'] == 'user' for r in result.records if r.source_id.startswith('received-user:'))
    index.forget('u', 'received-user:b')
    assert index.dependencies('u', ['reply:a'], before=NOW).blocked_source_ids == ('reply:a',)

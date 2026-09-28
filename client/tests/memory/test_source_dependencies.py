from datetime import datetime, timedelta, timezone

import pytest

from runtime.memory.source_retrieval import SourceRetrieval


NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


def fixture(tmp_path):
    store = SourceRetrieval(tmp_path / 'originals.db')
    for i, text in enumerate(('准备寄明信片', '明信片还在我手里', '现在已经寄出了')):
        store.put('owner', str(i), text, '收到你的消息', NOW + timedelta(minutes=i))
    return store


def save(store, first='0', second='1', **kwargs):
    return store.save_dependency('owner', first, second, 'user', 'user',
        kwargs.pop('earlier_quote', '准备寄明信片'), kwargs.pop('later_quote', '明信片还在我手里'),
        kwargs.pop('kind', 'correction'), **kwargs)


def test_chain_restart_retraction_is_durable(tmp_path):
    store = fixture(tmp_path)
    assert save(store)
    assert save(store, '1', '2', earlier_quote='明信片还在我手里', later_quote='现在已经寄出了', kind='state_change')
    store = SourceRetrieval(store.path)
    result = store.dependencies('owner', ['0'])
    assert {r.source_id for r in result.records} == {'0', '1', '2'}
    assert len(result.relations) == 2
    assert not result.blocked_source_ids
    identity = next(r['dependency_id'] for r in result.relations if r['earlier_source'] == '0')
    assert not store.retract_dependency('another', identity)
    assert store.retract_dependency('owner', identity)
    assert not save(store)
    store.put('owner', '1', '补充：明信片还在我手里', '收到你的消息', NOW + timedelta(minutes=1))
    assert not save(store)  # Editing surrounding text cannot resurrect a retracted candidate.
    assert not store.dependencies('owner', ['0']).relations


@pytest.mark.parametrize('damage', ['forget', 'edit', 'excluded', 'future'])
def test_incomplete_dependencies_block_seed(tmp_path, damage):
    store = fixture(tmp_path)
    assert save(store)
    kwargs = {}
    if damage == 'forget':
        store.forget('owner', '1')
    elif damage == 'edit':
        store.put('owner', '1', '完全不同的内容', '收到你的消息', NOW + timedelta(minutes=1))
    elif damage == 'excluded':
        kwargs['exclude_source_ids'] = ['1']
    else:
        kwargs['before'] = NOW
    result = store.dependencies('owner', ['0'], **kwargs)
    assert result.blocked_source_ids == ('0',)
    assert not result.records


@pytest.mark.parametrize('change', ['same', 'reverse', 'quote', 'kind', 'actor', 'naive', 'missing'])
def test_invalid_edges_are_not_saved(tmp_path, change):
    store = fixture(tmp_path)
    args = ['owner', '0', '1', 'user', 'user', '准备寄明信片', '明信片还在我手里', 'correction']
    if change == 'same': args[2] = '0'
    if change == 'reverse': args[1:3], args[5:7] = ['1', '0'], ['明信片还在我手里', '准备寄明信片']
    if change == 'quote': args[6] = '不存在的原话'
    if change == 'kind': args[7] = 'confirmed_fact'
    if change == 'actor': args[4] = 'someone'
    if change == 'missing': args[0] = 'another'
    if change == 'naive': store.put('owner', '1', args[6], '', NOW.replace(tzinfo=None))
    assert not store.save_dependency(*args)
    assert not store.dependencies('owner', ['0']).relations


def test_cycle_is_bounded_and_over_limit_blocks(tmp_path):
    store = fixture(tmp_path)
    for i in range(34):
        store.put('owner', f'n{i}', '原话文本', '', NOW)
    for i in range(33):
        assert store.save_dependency('owner', f'n{i}', f'n{i+1}', 'user', 'user', '原话文本', '原话文本', 'challenge')
    assert store.dependencies('owner', ['n0']).blocked_source_ids == ('n0',)
    assert store.save_dependency('owner', 'n33', 'n32', 'user', 'user', '原话文本', '原话文本', 'challenge')
    assert len(store.dependencies('owner', ['n32']).relations) == 2


def test_cutoff_inclusive_multiple_seeds_and_no_private_text_in_edges(tmp_path):
    store = fixture(tmp_path)
    assert save(store)
    store.put('owner', 'other', '另一个先前说法', '', NOW)
    assert store.save_dependency('owner', 'other', '1', 'user', 'user', '另一个先前说法', '明信片还在我手里', 'challenge')
    result = store.dependencies('owner', ['0', 'other'], before=NOW + timedelta(minutes=1))
    assert not result.blocked_source_ids
    assert len(result.relations) == 2
    assert {r.source_id for r in result.records} == {'0', 'other', '1'}
    assert all('quote' not in k and 'hash' not in k for edge in result.relations for k in edge)
    assert result.relations[0]['later_start'] == 0
    store.forget('owner', '1')
    assert set(store.dependencies('owner', ['0', 'other']).blocked_source_ids) == {'0', 'other'}


def test_unindexed_archive_is_not_blocked_but_forgotten_seed_is(tmp_path):
    store = fixture(tmp_path)
    assert not store.dependencies('owner', ['history:unindexed']).blocked_source_ids
    store.forget('owner', 'history:unindexed')
    assert store.dependencies('owner', ['history:unindexed']).blocked_source_ids == ('history:unindexed',)


def test_cross_user_unknown_time_and_speaker_are_not_inferred(tmp_path):
    store = fixture(tmp_path)
    store.put('other', 'foreign', '新的说法', '', NOW)
    assert not store.save_dependency('owner', '0', 'foreign', 'user', 'user', '准备寄明信片', '新的说法', 'correction')
    store.put('owner', 'undated', '新的说法', '', None)
    assert not store.save_dependency('owner', '0', 'undated', 'user', 'user', '准备寄明信片', '新的说法', 'correction')
    assert not store.save_dependency('owner', '0', '1', 'linli', 'user', '准备寄明信片', '明信片还在我手里', 'correction')
    assert store.save_dependency('owner', '0', '1', 'linli', 'linli', '收到你的消息', '收到你的消息', 'challenge')
    assert not store.dependencies('other', ['0']).relations


@pytest.mark.parametrize('stamp', [None, NOW.replace(tzinfo=None)])
def test_unrelated_indexed_unknown_time_is_retained_as_unknown(tmp_path, stamp):
    store = fixture(tmp_path)
    store.put('owner', 'archive', '没有可靠日期的原文', '原文回复', stamp)
    result = store.dependencies('owner', ['archive'], before=NOW)
    assert not result.blocked_source_ids
    assert len(result.records) == 2
    assert all(record.occurred_at is None for record in result.records)


def test_unrelated_known_future_is_still_blocked(tmp_path):
    store = fixture(tmp_path)
    assert store.dependencies('owner', ['2'], before=NOW).blocked_source_ids == ('2',)

import pytest
from runtime.imports.letter_backup import export_letters, import_letters, validate_backup
from runtime.memory.local_memory import LocalMemoryAdapter


def test_roundtrip_preserves_exact_text_time_and_identity(tmp_path):
    letters = [{'letter_id': 'local-1', 'content': '  杯底画了一只蜗牛。\r\n围巾是橙色的。\n',
                'reply_text': '记下了。\n\n林离', 'created_at': 1788000000,
                'replied_at': 1788000100, 'reply_mode': 'voice',
                'api_key': 'never-export', 'media_path': 'never-export'}]
    payload = export_letters(letters)
    assert 'never-export' not in str(payload)
    with LocalMemoryAdapter(tmp_path/'memory.sqlite3') as adapter:
        assert import_letters(payload, adapter=adapter)['inserted'] == 1
        assert import_letters(payload, adapter=adapter)['duplicates'] == 1
        copied = export_letters(adapter.list_legacy())
        assert copied['letters'] == payload['letters']
        assert copied['letters'][0]['content'] == letters[0]['content']
        assert import_letters(copied, adapter=adapter)['duplicates'] == 1


def test_same_machine_restore_does_not_duplicate_live_letters(tmp_path):
    letters = [{'letter_id':'live-1', 'content':'原信', 'reply_text':'回信'}]
    with LocalMemoryAdapter(tmp_path/'memory.sqlite3') as adapter:
        result = import_letters(export_letters(letters), adapter=adapter, existing=letters)
        assert result['inserted'] == 0 and result['duplicates'] == 1


def test_invalid_late_record_cannot_partially_import(tmp_path):
    payload = export_letters([{'letter_id':'one', 'content':'完整原信'}])
    payload['letters'].append({'content': 42})
    with LocalMemoryAdapter(tmp_path/'memory.sqlite3') as adapter:
        with pytest.raises(ValueError):
            import_letters(payload, adapter=adapter)
        assert not adapter.list_legacy()


def test_unknown_dates_stay_unknown_and_invalid_schema_is_rejected():
    payload = export_letters([{'content':'日期未知的旧信'}])
    assert validate_backup(payload)[0]['created_at'] is None
    with pytest.raises(ValueError):
        validate_backup({'schema_version':'other','letters':[]})


@pytest.mark.parametrize('value', [1790742840, '1790742840',
    '2026-09-30T12:34:00+08:00', '2026-09-30T04:34:00Z',
    '2026-09-30T13:34:00+09:00', '2026-09-30T12:34:00'])
@pytest.mark.parametrize('reverse', [False, True])
def test_soul_json_same_instant_deduplicates_in_both_orders(tmp_path, value, reverse):
    soul = {'format': 'soul', 'manifest': {'memory': {'exchanges': [
        {'incoming': 'synthetic', 'reply': 'reply', 'date': '2026-09-30', 'time': '12:34'}]}}}
    backup = {'schema_version': 'olivia.letters.v1', 'letters': [
        {'content': 'synthetic', 'reply_text': 'reply', 'created_at': value,
         'origin': 'user', 'reply_mode': 'text', 'letter_status': 'COMPLETED'}]}
    first, second = (backup, soul) if reverse else (soul, backup)
    with LocalMemoryAdapter(tmp_path / 'memory.sqlite3') as adapter:
        assert import_letters(first, adapter=adapter)['inserted'] == 1
        result = import_letters(second, adapter=adapter, existing=adapter.list_legacy())
        assert (result['inserted'], result['duplicates']) == (0, 1)
        assert len(adapter.list_legacy()) == 1


def test_existing_offset_backup_deduplicates_without_rewriting_archive(tmp_path):
    import json
    from runtime.memory.memory_port import LegacyLetter
    from runtime.imports.letter_backup import KIND, _record, identity
    old = _record({'content': 'synthetic', 'reply_text': 'reply',
                   'created_at': '2026-09-30T12:34:00+08:00',
                   'replied_at': '2026-09-30T12:35:00+08:00'})
    # Preserve the serialized representation emitted by earlier releases.
    old.update(created_at='2026-09-30T12:34:00+08:00', replied_at='2026-09-30T12:35:00+08:00')
    with LocalMemoryAdapter(tmp_path / 'memory.sqlite3') as adapter:
        adapter.import_legacy_records([LegacyLetter(content=json.dumps(old),
            source_record_id='letter-backup:' + identity(old), source='letter-backup',
            occurred_at=old['created_at'], metadata={'import_kind': KIND, 'backup_record': old,
            'user_content': old['content'], 'reply_text': old['reply_text']})], atomic=True)
        before = adapter.list_legacy()
        incoming = {**old, 'created_at': 1790742840, 'replied_at': 1790742900}
        result = import_letters({'schema_version': 'olivia.letters.v1', 'letters': [incoming]},
                                adapter=adapter, existing=before)
        assert (result['inserted'], result['duplicates']) == (0, 1)
        assert adapter.list_legacy() == before


def test_time_normalization_does_not_merge_different_instants(tmp_path):
    rows = [{'content': 'synthetic', 'reply_text': 'reply', 'created_at': value}
            for value in ('2026-09-30T12:34:00+08:00', '2026-09-30T12:34:00Z', None)]
    with LocalMemoryAdapter(tmp_path / 'memory.sqlite3') as adapter:
        result = import_letters({'schema_version': 'olivia.letters.v1', 'letters': rows}, adapter=adapter)
        assert result['inserted'] == 3

import asyncio
from copy import deepcopy

import pytest


@pytest.fixture
def server(tmp_path, monkeypatch):
    import local_server as s
    monkeypatch.setattr(s, 'store', s.Store())
    monkeypatch.setattr(s, '_state_root', lambda: tmp_path)
    monkeypatch.setattr(s, '_store_state_error_code', None)
    monkeypatch.setattr(s, '_official_history_mailbox_projection', lambda **kw: [])
    monkeypatch.setattr(s, '_mark_superseded_failed_retries', lambda: None)
    s.store.letters = [dict(letter_id=id, content='去信', reply_text='回信',
                            letter_status='COMPLETED', created_at=100) for id in ('a', 'b')]
    return s


def call(s, endpoint, body=None, confirmed=True):
    return asyncio.run(s.route('POST', '/toy/letter/maintenance/' + endpoint,
                              body or {}, {}, companion_confirmed=confirmed))


def test_confirmed_preview_apply_restart_restore_and_export(server):
    s = server
    original = deepcopy(s.store.letters)
    assert call(s, 'preview', confirmed=False)['code'] == 403
    plan = call(s, 'preview')['data']
    assert '_changes' not in plan
    assert s.store.letter_maintenance == {}
    choice = next(i for i in plan['items'] if i['kind'] == 'duplicate')['options'][0]['id']
    result = call(s, 'apply', {'token': plan['token'], 'selected': [choice]})
    assert result['data']['changed'] == 1
    assert len(s._letter_collection('current')) == 1
    assert s.store.letters == original
    s.store = s.Store()
    s._load_store_state()
    assert len(s._letter_collection('current')) == 1
    plan = call(s, 'preview')['data']
    restore = next(i for i in plan['items'] if i['kind'] == 'restore')['options'][0]['id']
    assert call(s, 'apply', {'token': plan['token'], 'selected': [restore]})['code'] == 0
    assert len(s._letter_collection('current')) == 2


def test_stale_preview_forged_selection_and_busy_do_not_write(server):
    plan = call(server, 'preview')['data']
    server.store.letters[0]['content'] = '刚刚改过'
    assert call(server, 'apply', {'token': plan['token'], 'selected': []})['code'] == 409
    plan = call(server, 'preview')['data']
    assert call(server, 'apply', {'token': plan['token'], 'selected': ['forged']})['code'] == 400
    server._history_memory_admin_gate.acquire()
    try:
        assert call(server, 'preview')['code'] == 409
    finally:
        server._history_memory_admin_gate.release()
    assert not server.store.letter_maintenance


def test_preview_does_not_run_mailbox_auto_cleanup(server, monkeypatch):
    def forbidden():
        raise AssertionError('preview must not mutate failed retry records')
    monkeypatch.setattr(server, '_mark_superseded_failed_retries', forbidden)
    assert call(server, 'preview')['data']['status'] == 'READY'


def test_write_failure_keeps_live_originals_and_projection(server, monkeypatch):
    plan = call(server, 'preview')['data']
    selected = [plan['items'][0]['options'][0]['id']]
    def fail():
        raise server.StoreStateUnavailable()
    monkeypatch.setattr(server, '_persist_store_state', fail)
    assert call(server, 'apply', {'token': plan['token'], 'selected': selected})['code'] == 503
    assert not server.store.letter_maintenance
    assert len(server._letter_collection('current')) == 2


def test_preview_pagination_full_detail_and_invalid_source(server):
    server.store.letters = [dict(letter_id=str(i), content='全文' * 250, reply_text='',
                                letter_status='FAILED', created_at=i) for i in range(35)]
    plan = call(server, 'preview')['data']
    assert len(plan['items']) == 30
    assert plan['total'] == 35
    detail = call(server, 'detail', {'key': plan['items'][0]['left']['key']})
    assert len(detail['data']['content']) == 500
    assert len(call(server, 'preview', {'page': 1})['data']['items']) == 5
    assert call(server, 'preview', {'backup': {'invalid': True}})['code'] == 400


def test_repaired_native_letter_still_marks_original_read(server):
    from runtime.imports.letter_maintenance import key
    server.store.letters = [dict(letter_id='native', content='正文', reply_text='回信',
                                letter_status='COMPLETED', created_at=100, is_read=0)]
    row = server.store.letters[0]
    server.store.letter_maintenance = {key(row): {'created_at': 200, 'order': 0}}
    detail = asyncio.run(server.route('GET', '/toy/letter/detail', {}, {'letter_id': 'native'}))
    assert detail['data']['created_at'] == 200
    assert row['is_read'] == 1


def test_repaired_archive_export_preserves_text_and_sqlite(server, tmp_path, monkeypatch):
    from runtime.memory.local_memory import LocalMemoryAdapter
    from runtime.imports.letter_backup import import_letters, export_letters
    from runtime.imports.letter_maintenance import key
    archive = LocalMemoryAdapter(tmp_path / 'memory.sqlite3')
    try:
        payload = export_letters([{'letter_id': 'old', 'content': '旧的正文', 'reply_text': '原始回信'}])
        import_letters(payload, adapter=archive)
        row = archive.list_legacy()[0]
        original = deepcopy(row)
        server.store.letters = []
        monkeypatch.setattr(server, '_official_history_mailbox_projection', lambda **kw: [row])
        server.store.letter_maintenance = {key(row): {'created_at': 1790742840, 'order': 0}}
        exported = export_letters(server._letter_collection('current'))['letters'][0]
        assert exported['content'] == '旧的正文'
        assert exported['reply_text'] == '原始回信'
        assert exported['created_at'] == '2026-09-30T04:34:00+00:00'
        assert archive.list_legacy()[0] == original
        server.store.letter_maintenance[key(row)]['hidden'] = 'duplicate'
        monkeypatch.setattr(server, '_legacy_import_adapter', lambda: archive)
        monkeypatch.setattr(server, '_start_history_relationships', lambda: None)
        for backup in (payload, {'schema_version': payload['schema_version'], 'letters': [exported]}):
            result = asyncio.run(server.route('POST', '/toy/letter/backup/import',
                                 {'backup': backup}, {}, companion_confirmed=True))
            assert result['data']['inserted'] == 0
            assert result['data']['duplicates'] == 1
    finally:
        archive.close()

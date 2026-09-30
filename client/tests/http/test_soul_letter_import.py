import asyncio
from types import SimpleNamespace

import pytest

from runtime.imports.letter_backup import import_letters
from runtime.memory.local_memory import LocalMemoryAdapter


def soul(*exchanges):
    return {'format': 'soul', 'manifest': {'memory': {'exchanges': list(exchanges)}}}


def exchange(content='synthetic one', reply='synthetic reply'):
    return {'incoming': content, 'reply': reply, 'date': '2026-09-30', 'time': '12:34'}


def test_soul_route_preserves_text_time_order_and_deduplicates(tmp_path, monkeypatch):
    import local_server as server
    with LocalMemoryAdapter(tmp_path / 'memory.sqlite3') as archive:
        monkeypatch.setattr(server, 'store', SimpleNamespace(letters=[], legacy_letters=[]))
        monkeypatch.setattr(server, 'memory_adapter', archive)
        monkeypatch.setattr(server, '_legacy_import_adapter', lambda: archive)
        monkeypatch.setattr(server, '_mark_superseded_failed_retries', lambda: None)
        monkeypatch.setattr(server, '_start_history_relationships', lambda **_: None)
        payload = soul(exchange('first\nline'), exchange('second'))
        async def run():
            result = await server.route('POST', '/toy/letter/backup/import', {'backup': payload}, {}, companion_confirmed=True)
            assert result['code'] == 0
            assert result['data']['inserted'] == 2
            mailbox = server._letter_collection('current')
            assert [x['content'] for x in mailbox] == ['first\nline', 'second']
            assert all(isinstance(x['created_at'], (int, float)) for x in mailbox)
            assert mailbox[0]['created_at'] == 1790742840
            repeated = await server.route('POST', '/toy/letter/backup/import', {'backup': payload}, {}, companion_confirmed=True)
            assert repeated['data']['inserted'] == 0
            assert repeated['data']['duplicates'] == 2
        asyncio.run(run())


def test_soul_rejects_bad_row_before_any_write(tmp_path):
    with LocalMemoryAdapter(tmp_path / 'memory.sqlite3') as archive:
        with pytest.raises(ValueError):
            import_letters(soul(exchange(), {'incoming': ['invalid'], 'reply': 'reply'}), adapter=archive)
        assert archive.list_legacy() == []


def test_soul_skips_existing_text_without_rewriting_it(tmp_path):
    with LocalMemoryAdapter(tmp_path / 'memory.sqlite3') as archive:
        result = import_letters(soul(exchange()), adapter=archive,
            existing=[{'content': 'synthetic one', 'reply_text': 'synthetic reply'}])
        assert result['inserted'] == 0
        assert result['duplicates'] == 1


def test_soul_unknown_date_stays_unknown_and_invalid_manifest_is_atomic(tmp_path):
    with LocalMemoryAdapter(tmp_path / 'memory.sqlite3') as archive:
        for manifest in (None, {}, {'memory': {'exchanges': {}}}, {'memory': {'exchanges': [None]}}):
            with pytest.raises(ValueError):
                import_letters({'format': 'soul', 'manifest': manifest}, adapter=archive)
        assert archive.list_legacy() == []
        result = import_letters(soul({**exchange(), 'date': 'unknown'}), adapter=archive)
        assert result['inserted'] == 1
        assert archive.list_legacy()[0]['occurred_at'] is None


def test_soul_contract_and_record_limit(tmp_path):
    import json
    from pathlib import Path
    import jsonschema
    schema = json.loads((Path(__file__).parents[2] / 'contracts/soul_letter_import.schema.json').read_text())
    jsonschema.validate(soul(exchange()), schema)
    with LocalMemoryAdapter(tmp_path / 'memory.sqlite3') as archive:
        with pytest.raises(ValueError):
            import_letters(soul(*[exchange()] * 10001), adapter=archive)
        with pytest.raises(ValueError):
            import_letters(soul({**exchange(), 'incoming': []}), adapter=archive)
        assert archive.list_legacy() == []

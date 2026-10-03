import asyncio
import json
import sqlite3

from private_world_ledger import SQLitePrivateWorldLedger
from private_world_service import PrivateWorldCommandService
from runtime.imports import relationship_batches as batches
from tests.imports.test_relationship_batches import Gateway, rows


class SafeContextError(RuntimeError):
    code = 'HISTORY_RELATIONSHIP_UNAVAILABLE'
    failure_context = {
        'cause_code': 'INPUT_TOO_LONG', 'failure_stage': 'gateway',
        'exception_type': 'InvalidGatewayInput', 'input_chars': 11001,
        'max_input_chars': 10000, 'input_bytes': 22002, 'max_input_bytes': 30000,
        'exchange_count': 999, 'batch_id': 999,
        'message': 'private-error-with-key', 'content': 'private-letter',
        'api_key': 'private-key', 'path': 'C:/private/path',
    }


class BoundedGateway(Gateway):
    async def complete(self, messages, **kwargs):
        result = await super().complete(messages, **kwargs)
        assessment = json.loads(result.text)
        for name in ('familiarity', 'trust', 'comfort', 'closeness'):
            assessment[name] = min(assessment[name], 100)
        result.text = json.dumps(assessment)
        return result


def _run(queue, gateway, service, ledger):
    asyncio.run(queue.run(gateway=gateway, persona_policy='policy',
                         command_service=service, snapshot=ledger.snapshot))


def test_legacy_queue_database_adds_failure_context_column_idempotently(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE batches (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, '
                   'state TEXT NOT NULL, assessment TEXT, error TEXT)')
        db.execute("INSERT INTO batches(payload,state,error) VALUES ('[]','failed','HISTORY_RELATIONSHIP_UNAVAILABLE')")
    for _ in range(2):
        queue = batches.RelationshipBatches(path)
        with queue.connect() as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(batches)')]
        assert columns.count('failure_context') == 1
        assert queue.status()['failure_context'] == {}


def test_failed_10_of_165_context_survives_restart_and_explicit_retry(tmp_path, monkeypatch):
    path = tmp_path / 'queue.sqlite3'
    queue = batches.RelationshipBatches(path)
    queue.enqueue(batches.archive_exchanges(rows(165)))
    ledger = SQLitePrivateWorldLedger(tmp_path / 'world.sqlite3')
    service = PrivateWorldCommandService(ledger)
    original = batches.assess_historical_relationship
    calls = []

    async def assess(exchanges, **kwargs):
        calls.append(True)
        if len(calls) == 3:
            raise SafeContextError('private-error-with-key')
        return await original(exchanges, **kwargs)

    monkeypatch.setattr(batches, 'assess_historical_relationship', assess)
    gateway = BoundedGateway()
    _run(queue, gateway, service, ledger)
    expected = {**{key: value for key, value in SafeContextError.failure_context.items()
                   if key in {'cause_code', 'failure_stage', 'exception_type', 'input_chars',
                              'max_input_chars', 'input_bytes', 'max_input_bytes'}},
                'batch_id': 3, 'exchange_count': 5}
    reopened = batches.RelationshipBatches(path)
    assert reopened.status()['status'] == 'FAILED'
    assert reopened.status()['processed'] == 10 and reopened.status()['total'] == 165
    assert reopened.status()['failure_context'] == expected
    with reopened.connect() as db:
        error, context = db.execute("SELECT error,failure_context FROM batches WHERE state='failed'").fetchone()
    assert error == SafeContextError.code and json.loads(context) == expected
    assert 'private-' not in context
    _run(reopened, gateway, service, ledger)
    assert len(calls) == 3  # Failed durable batches require an explicit retry.
    reopened.retry()
    assert 'failure_context' not in reopened.status()
    assert reopened.status()['error_code'] is None
    with reopened.connect() as db:
        assert db.execute('SELECT failure_context FROM batches WHERE id=3').fetchone()[0] is None
    _run(reopened, gateway, service, ledger)
    assert reopened.status()['status'] == 'APPLIED' and reopened.status()['processed'] == 165
    assert 'failure_context' not in reopened.status()


def test_failure_context_is_projected_again_when_loading_dirty_database(tmp_path):
    queue = batches.RelationshipBatches(tmp_path / 'queue.sqlite3')
    queue.enqueue(batches.archive_exchanges(rows(5)))
    dirty = {'cause_code': 'private-secret', 'failure_stage': 'private-path',
             'exception_type': 'private-class', 'input_chars': True,
             'max_input_chars': -1, 'input_bytes': 1_000_000_001,
             'exchange_count': 5, 'batch_id': 1, 'message': 'private-letter'}
    with queue.connect() as db:
        db.execute("UPDATE batches SET state='failed',error='HISTORY_RELATIONSHIP_UNAVAILABLE',failure_context=?",
                   (json.dumps(dirty),))
    assert queue.status()['failure_context'] == {'batch_id': 1, 'exchange_count': 5}
    with queue.connect() as db:
        db.execute("UPDATE batches SET failure_context='not-json-private-error'")
    assert queue.status()['failure_context'] == {}


def test_successful_recovery_removes_old_failure_context(tmp_path):
    queue = batches.RelationshipBatches(tmp_path / 'queue.sqlite3')
    queue.enqueue(batches.archive_exchanges(rows(5)))
    with queue.connect() as db:
        db.execute('UPDATE batches SET failure_context=?', (json.dumps({'cause_code': 'INPUT_TOO_LONG'}),))
    ledger = SQLitePrivateWorldLedger(tmp_path / 'world.sqlite3')
    _run(queue, Gateway(), PrivateWorldCommandService(ledger), ledger)
    assert queue.status()['status'] == 'APPLIED' and 'failure_context' not in queue.status()
    with queue.connect() as db:
        assert db.execute('SELECT failure_context FROM batches').fetchone()[0] is None


def test_unannotated_commit_failure_gets_finite_context_without_exception_text(tmp_path):
    queue = batches.RelationshipBatches(tmp_path / 'queue.sqlite3')
    queue.enqueue(batches.archive_exchanges(rows(5)))
    ledger = SQLitePrivateWorldLedger(tmp_path / 'world.sqlite3')

    class BrokenCommandService:
        def lookup_command(self, command):
            raise RuntimeError('private-command-error-with-key')

    gateway = Gateway()
    _run(queue, gateway, BrokenCommandService(), ledger)
    assert queue.status()['error_code'] == 'HISTORY_RELATIONSHIP_FAILED'
    assert queue.status()['failure_context'] == {
        'cause_code': 'UNKNOWN', 'failure_stage': 'commit',
        'exception_type': 'RuntimeError', 'batch_id': 1, 'exchange_count': 5,
    }
    assert gateway.calls == []
    with queue.connect() as db:
        error, context = db.execute('SELECT error,failure_context FROM batches').fetchone()
    assert 'private-' not in error + context

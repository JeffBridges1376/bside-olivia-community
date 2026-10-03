import io
import json
import zipfile

import pytest

from runtime.diagnostics.support_bundle import build_diagnostic_bundle, project_history_relationship_failure
from runtime.imports.relationship_batches import RelationshipBatches, archive_exchanges
from tests.imports.test_relationship_batches import rows


def _failed_queue(tmp_path):
    path = tmp_path / 'queue.sqlite3'
    queue = RelationshipBatches(path)
    queue.enqueue(archive_exchanges(rows(165)))
    context = {'cause_code': 'INPUT_TOO_LONG', 'failure_stage': 'gateway',
               'exception_type': 'InvalidGatewayInput', 'input_chars': 11001,
               'max_input_chars': 10000, 'batch_id': 3, 'exchange_count': 5,
               'key': 'private-secret', 'message': 'private-letter'}
    with queue.connect() as db:
        db.execute("UPDATE batches SET state='done' WHERE id<3")
        db.execute("UPDATE batches SET state='failed',error='HISTORY_RELATIONSHIP_UNAVAILABLE',failure_context=? WHERE id=3",
                   (json.dumps(context),))
    return RelationshipBatches(path)


def test_restarted_queue_failure_exports_through_real_collector_without_recent_ring(tmp_path, monkeypatch):
    import local_server
    import original_client_server

    queue = _failed_queue(tmp_path)
    monkeypatch.setattr(local_server, '_history_relationship_queue', queue)
    monkeypatch.setattr(local_server, '_history_relationship_task', None)
    monkeypatch.setattr(local_server, '_local_import_task', None)
    monkeypatch.setattr(local_server, '_local_import_result', None)
    monkeypatch.setattr(local_server, 'runtime_diagnostic_event_snapshot', lambda: ())
    assert local_server._history_relationship_status()['processed'] == 10
    collectors = []
    monkeypatch.setattr(original_client_server, 'mount_original_client_diagnostics_api',
                        lambda app, collect, **kwargs: collectors.append(collect))
    original_client_server.create_configured_original_client_server_runtime(server_module=local_server, environ={})
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(collectors[0]()))) as archive:
        exported = [json.loads(line) for line in archive.read('runtime-tail.jsonl').splitlines()]
        failure = next(row for row in exported if row['event'] == 'history_relationship_failed')
        assert failure == {
            'event': 'history_relationship_failed', 'status': 'failed',
            'error_code': 'HISTORY_RELATIONSHIP_UNAVAILABLE', 'total': 165, 'processed': 10,
            'failure_context': {'cause_code': 'INPUT_TOO_LONG', 'failure_stage': 'gateway',
                                'exception_type': 'InvalidGatewayInput', 'input_chars': 11001,
                                'max_input_chars': 10000, 'batch_id': 3, 'exchange_count': 5},
        }
        assert all(b'private-' not in archive.read(name) for name in archive.namelist())
    queue.retry()
    assert not any(row['event'] == 'history_relationship_failed'
                   for row in collectors[0]()['runtime_tail'])


@pytest.mark.parametrize('queue', [None, 'broken'])
def test_queue_unready_or_broken_does_not_prevent_diagnostic_export(monkeypatch, queue):
    import local_server

    class BrokenQueue:
        def status(self):
            raise OSError('private-database-path')

    monkeypatch.setattr(local_server, '_history_relationship_queue', BrokenQueue() if queue else None)
    assert local_server._history_relationship_diagnostic_snapshot() == ()


def test_history_failure_tail_projection_keeps_only_finite_context():
    raw = {'event': 'history_relationship_failed', 'status': 'FAILED',
           'error_code': 'HISTORY_RELATIONSHIP_UNAVAILABLE', 'total': 165, 'processed': 10,
           'cause_code': 'JEV_PRIVATE_API_KEY', 'cause_type': 'RuntimeError',
           'failure_context': {'cause_code': 'INPUT_TOO_LONG', 'failure_stage': 'gateway',
                               'exception_type': 'InvalidGatewayInput', 'batch_id': 3,
                               'exchange_count': 5, 'input_chars': True, 'content': 'private-letter'},
           'key': 'private-secret', 'message': 'private-error'}
    source = {'summary': {'status': 'available'}, 'health': {'status': 'available', 'checks': {}},
              'install': {'status': 'available'}, 'tasks': {'status': 'available', 'pending': 0, 'items': []},
              'launcher_tail': [], 'runtime_tail': [raw]}
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        projected = json.loads(archive.read('runtime-tail.jsonl'))
        assert projected['failure_context'] == {'cause_code': 'INPUT_TOO_LONG',
                                               'failure_stage': 'gateway',
                                               'exception_type': 'InvalidGatewayInput',
                                               'batch_id': 3, 'exchange_count': 5}
        assert 'cause_code' not in projected and 'cause_type' not in projected
        assert b'JEV_PRIVATE_API_KEY' not in archive.read('runtime-tail.jsonl')
        assert b'private-' not in archive.read('runtime-tail.jsonl')


@pytest.mark.parametrize('code', ['JEV_PRIVATE_API_KEY', 'PRIVATE_WORLD_PRIVATE_API_KEY',
                                'PRIVATE_WORLD_HISTORY_JEV_PRIVATE_API_KEY', 'private-error-path', None])
def test_history_failure_outer_code_is_finite_in_snapshot_ring_and_bundle(code):
    import local_server

    raw = {'event': 'history_relationship_failed', 'status': 'FAILED', 'error_code': code,
           'failure_context': {'cause_code': 'INPUT_TOO_LONG', 'failure_stage': 'gateway'}}
    expected = {**raw, 'error_code': 'HISTORY_RELATIONSHIP_FAILED'}
    assert project_history_relationship_failure(raw) == expected
    assert local_server._runtime_diagnostic_record(raw['event'], raw) == expected
    source = {'summary': {'status': 'available'}, 'health': {'status': 'available', 'checks': {}},
              'install': {'status': 'available'}, 'tasks': {'status': 'available', 'pending': 0, 'items': []},
              'launcher_tail': [], 'runtime_tail': [raw]}
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        exported = json.loads(archive.read('runtime-tail.jsonl'))
        assert exported == {**expected, 'status': 'failed'}


@pytest.mark.parametrize('code', ['HISTORY_RELATIONSHIP_UNAVAILABLE',
                                'HISTORY_RELATIONSHIP_STORAGE_UNAVAILABLE',
                                'PRIVATE_WORLD_HISTORY_LLM_INPUT_TOO_LONG',
                                'PRIVATE_WORLD_HISTORY_LLM_QUOTA_EXHAUSTED',
                                'PRIVATE_WORLD_HISTORY_JEV_TIMEOUT',
                                'PRIVATE_WORLD_HISTORY_JEV_BILLING_HTTP_403',
                                'PRIVATE_WORLD_HISTORY_INITIALIZATION_FAILED',
                                'PRIVATE_WORLD_HISTORY_LOOKUP_FAILED',
                                'PRIVATE_WORLD_HISTORY_PREPARE_FAILED',
                                'PRIVATE_WORLD_HISTORY_WRITE_FAILED',
                                'PRIVATE_WORLD_COMMAND_STORAGE_UNAVAILABLE',
                                'PRIVATE_WORLD_COMMAND_IDENTITY_CONFLICT'])
def test_history_failure_outer_code_keeps_known_assessment_import_and_commit_codes(code):
    assert project_history_relationship_failure({'status': 'FAILED', 'error_code': code})['error_code'] == code


def test_recorded_failure_context_survives_manual_retry_in_ring_and_bundle(tmp_path, monkeypatch):
    from collections import deque
    import local_server

    queue = _failed_queue(tmp_path)
    monkeypatch.setattr(local_server, '_history_relationship_queue', queue)
    monkeypatch.setattr(local_server, '_RUNTIME_DIAGNOSTIC_EVENTS', deque(maxlen=160))
    monkeypatch.setattr(local_server, '_RUNTIME_REQUEST_EVENTS', deque(maxlen=40))
    event = local_server._history_relationship_diagnostic_snapshot()[0]
    local_server._safe_log(event['event'], **{key: value for key, value in event.items() if key != 'event'})
    queue.retry()
    assert local_server._history_relationship_diagnostic_snapshot() == ()
    stored = local_server.runtime_diagnostic_event_snapshot()
    assert stored == (event,)
    source = {'summary': {'status': 'available'}, 'health': {'status': 'available', 'checks': {}},
              'install': {'status': 'available'}, 'tasks': {'status': 'available', 'pending': 0, 'items': []},
              'launcher_tail': [], 'runtime_tail': stored}
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        exported = json.loads(archive.read('runtime-tail.jsonl'))
        assert exported['failure_context'] == event['failure_context']
        assert b'private-' not in archive.read('runtime-tail.jsonl')

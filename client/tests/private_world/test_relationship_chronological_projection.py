"""Canonical relation consumers must project event time, not completion order."""
from datetime import datetime, timedelta, timezone
import hashlib
import itertools
import json
import sqlite3

import pytest

from runtime.private_world.ledger import SQLitePrivateWorldLedger
from runtime.memory.private_world_delivery import DeliveryEvent, PrivateWorldDeliveryCommitter
from runtime.memory.private_world_relationship import PrivateWorldRelationshipCommitter
from runtime.memory.private_world_relationship import RelationshipFactCommand
from runtime.private_world.ledger import LedgerEvent
from runtime.private_world.port import PrivateWorldSnapshot
from runtime.private_world.reducer import ReducerEventKind
from runtime.private_world.commands import (GrantIntimacy, GrantNickname, PrivateWorldActor,
                                           PrivateWorldCommandSource, InitializeHistoricalRelationship)
from runtime.private_world.service import PrivateWorldCommandService
from runtime.reply.reply_context import IntimacyTier, RelationshipStage

NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


def canonical(ledger, identity, at):
    PrivateWorldDeliveryCommitter(ledger).commit(DeliveryEvent(
        delivery_id=identity, occurred_at=at, semantic_key=identity,
        canonical_reply_sha256=hashlib.sha256(b'reply').hexdigest()))


def consume(ledger, identity, at, kind):
    return PrivateWorldRelationshipCommitter(ledger).commit_exchange(
        identity, identity, 'reply', {'kind': kind, 'user_quote': identity, 'reply_quote': 'reply'},
        occurred_at=at)


def state(ledger):
    value = ledger.snapshot().to_dict()
    value.pop('version')
    return value


def test_late_support_and_conflict_have_the_same_projection_after_restart(tmp_path):
    states = []
    for name, order in [('forward', (0, 1)), ('reverse', (1, 0))]:
        path = tmp_path / (name + '.db')
        ledger = SQLitePrivateWorldLedger(path)
        events = [('support', NOW, 'support_received'),
                  ('conflict', NOW + timedelta(minutes=1), 'conflict')]
        for identity, at, _ in events:
            canonical(ledger, identity, at)
        for index in order:
            identity, at, kind = events[index]
            assert consume(ledger, identity, at, kind).value == 'COMMITTED'
            ledger = SQLitePrivateWorldLedger(path)
        before = state(ledger)
        assert consume(ledger, *events[0]).value == 'DUPLICATE'
        assert state(ledger) == before
        states.append(before)
    assert states[0] == states[1]
    assert states[0]['tension'] == 3
    assert states[0]['trust'] == 0


@pytest.mark.parametrize('same_time', [False, True])
def test_stable_ties_and_equivalent_evidence_are_order_independent(tmp_path, same_time):
    snapshots = []
    for index, order in enumerate(itertools.permutations(range(3))):
        ledger = SQLitePrivateWorldLedger(tmp_path / f'order-{index}.db')
        events = [('first', NOW, 'support_received'),
                  ('second', NOW if same_time else NOW + timedelta(minutes=1), 'support_received'),
                  ('third', NOW + timedelta(minutes=2), 'conflict')]
        for identity, at, _ in events:
            canonical(ledger, identity, at)
        for i in order:
            identity, at, kind = events[i]
            # Distinct deliveries of the same support evidence share cooldown.
            assert PrivateWorldRelationshipCommitter(ledger).commit_exchange(
                identity, 'same evidence', 'reply', {'kind': kind,
                    'user_quote': 'same evidence', 'reply_quote': 'reply'}, occurred_at=at).value == 'COMMITTED'
        snapshots.append(state(ledger))
    assert all(item == snapshots[0] for item in snapshots)
    assert snapshots[0]['familiarity'] == 1


def test_legacy_baseline_is_preserved_without_guessing_old_inputs(tmp_path):
    snapshots = []
    for suffix, order in [('forward', (0, 1)), ('reverse', (1, 0))]:
        path = tmp_path / (suffix + '.db')
        ledger = SQLitePrivateWorldLedger(path)
        ledger.apply_once(LedgerEvent('legacy', 'legacy', 'opaque_legacy',
            {'applied': True, 'change_fields': ['relationship_stage']}, (NOW - timedelta(days=1)).isoformat()),
            PrivateWorldSnapshot(version=2, trust=10, comfort=10, familiarity=12,
                                 relationship_stage='close', nickname_permissions=('old-name',)))
        events = [('support', NOW, 'support_received'), ('conflict', NOW + timedelta(minutes=1), 'conflict')]
        for identity, at, _ in events:
            canonical(ledger, identity, at)
        for i in order:
            assert consume(ledger, *events[i]).value == 'COMMITTED'
        reopened = SQLitePrivateWorldLedger(path)
        snapshots.append(state(reopened))
        assert reopened.snapshot().relationship_stage == 'close'
        assert reopened.snapshot().nickname_permissions == ('old-name',)
        with sqlite3.connect(path) as db:
            baseline = json.loads(db.execute("SELECT value FROM private_world_metadata WHERE key='ordered_projection_v1'").fetchone()[0])
            assert json.loads(baseline['snapshot'])['trust'] == 10
            assert db.execute('SELECT COUNT(*) FROM private_world_projection_inputs').fetchone()[0] == 2
    assert snapshots[0] == snapshots[1]
    assert snapshots[0]['trust'] == 9


def common(identity, at):
    return dict(command_id=identity, idempotency_key=identity,
        actor=PrivateWorldActor.LOCAL_USER, source=PrivateWorldCommandSource.CONTROL_CENTER,
        occurred_at=at, reason='synthetic confirmed change', evidence_refs=())


def stage(ledger, identity, at, target):
    canonical(ledger, identity, at)
    return PrivateWorldRelationshipCommitter(ledger).commit(RelationshipFactCommand(
        command_id=identity + '.stage', kind=ReducerEventKind.STAGE_CONFIRMED,
        occurred_at=at, semantic_key=identity + '.stage', canonical_delivery_id=identity,
        canonical_reply_sha256=hashlib.sha256(b'reply').hexdigest(),
        evidence_ref_id=identity + '.evidence', target_stage=target))


def test_late_stage_cannot_retroactively_authorize_a_denied_grant(tmp_path):
    ledger = SQLitePrivateWorldLedger(tmp_path / 'permissions.db')
    service = PrivateWorldCommandService(ledger)
    denied = service.execute(GrantIntimacy(**common('grant', NOW + timedelta(minutes=1)),
        grant_id='grant.private', tier=IntimacyTier.LIGHT_CONTACT, statement='Private consent text'))
    assert denied.reason_code == 'INTIMACY_EXCEEDS_STAGE'
    assert stage(ledger, 'close', NOW, 'close').value == 'COMMITTED'
    assert ledger.snapshot().relationship_stage == 'close'
    assert ledger.snapshot().intimacy_grants == ()
    assert 'Private consent text' not in repr(ledger.events())
    assert 'grant.private' not in repr(ledger.events())
    assert SQLitePrivateWorldLedger(tmp_path / 'permissions.db').snapshot().intimacy_grants == ()


def test_late_stage_and_nickname_preserve_latest_stage_and_control_state(tmp_path):
    ledger = SQLitePrivateWorldLedger(tmp_path / 'stages.db')
    assert stage(ledger, 'new-stage', NOW + timedelta(hours=1), 'familiar').value == 'COMMITTED'
    PrivateWorldCommandService(ledger).execute(GrantNickname(**common('nickname', NOW + timedelta(minutes=30)), nickname='private-name'))
    assert stage(ledger, 'old-stage', NOW, 'close').value == 'COMMITTED'
    assert ledger.snapshot().relationship_stage == 'familiar'
    assert ledger.snapshot().nickname_permissions == ('private-name',)
    assert ledger.snapshot().intimacy_grants == ()


def test_rollback_writer_state_becomes_a_compatibility_checkpoint(tmp_path):
    from dataclasses import replace
    path = tmp_path / 'rollback.db'
    ledger = SQLitePrivateWorldLedger(path)
    canonical(ledger, 'first', NOW)
    assert consume(ledger, 'first', NOW, 'support_received').value == 'COMMITTED'
    # Simulate the old binary: no replay-input row and no new metadata update.
    saved = replace(ledger.snapshot(), version=ledger.snapshot().version + 1,
                    nickname_permissions=('kept-after-rollback',), trust=20)
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO private_world_events VALUES (?,?,?,?,?)',
                   ('old-writer', 'old-writer', 'old_control', '{}', (NOW + timedelta(minutes=1)).isoformat()))
        db.execute('INSERT INTO private_world_snapshots VALUES (?,?,?)',
                   (saved.version, ledger._snapshot_json(saved), 'old-writer'))
    reopened = SQLitePrivateWorldLedger(path)
    canonical(reopened, 'last', NOW + timedelta(minutes=2))
    assert consume(reopened, 'last', NOW + timedelta(minutes=2), 'conflict').value == 'COMMITTED'
    assert reopened.snapshot().trust == 18
    assert reopened.snapshot().nickname_permissions == ('kept-after-rollback',)


def test_corrupt_frozen_input_fails_without_partial_new_event(tmp_path):
    path = tmp_path / 'invalid.db'
    ledger = SQLitePrivateWorldLedger(path)
    canonical(ledger, 'first', NOW)
    assert consume(ledger, 'first', NOW, 'support_received').value == 'COMMITTED'
    canonical(ledger, 'last', NOW + timedelta(minutes=1))
    before = state(ledger)
    count = len(ledger.events())
    with sqlite3.connect(path) as db:
        db.execute("UPDATE private_world_projection_inputs SET payload_json='{}'")
    assert consume(ledger, 'last', NOW + timedelta(minutes=1), 'conflict').value == 'UNAVAILABLE'
    assert state(ledger) == before
    assert len(ledger.events()) == count


@pytest.mark.parametrize('score', [0, 20])
def test_accepted_import_baseline_survives_an_earlier_late_event(tmp_path, score):
    ledger = SQLitePrivateWorldLedger(tmp_path / 'import.db')
    service = PrivateWorldCommandService(ledger)
    imported = InitializeHistoricalRelationship(
        **{**common('initialize', NOW), 'actor': PrivateWorldActor.MIGRATION,
           'source': PrivateWorldCommandSource.IMPORT, 'evidence_refs': ('source.import',)},
        relationship_stage=RelationshipStage.UNKNOWN, familiarity=score, trust=score,
        comfort=score, closeness=0, tension=0)
    result = service.execute(imported)
    assert result.status.value == 'APPLIED'
    assert ledger.snapshot().version == 2
    at = NOW - timedelta(hours=1)
    canonical(ledger, 'late', at)
    assert consume(ledger, 'late', at, 'conflict').value == 'COMMITTED'
    assert ledger.snapshot().familiarity == score
    assert ledger.snapshot().trust == max(score - 2, 0)
    assert ledger.snapshot().tension == 3


def test_projection_refreshes_cached_contact_without_mutating_audit(tmp_path):
    from runtime.personal_chat.contact_invitation import observe
    path = tmp_path / 'contact.db'
    ledger = SQLitePrivateWorldLedger(path)
    ledger.apply_once(LedgerEvent('legacy', 'legacy', 'opaque', {}, (NOW - timedelta(days=1)).isoformat()),
        PrivateWorldSnapshot(version=2, trust=69, comfort=69, familiarity=70, closeness=70))
    later = NOW + timedelta(minutes=1)
    canonical(ledger, 'support', later)
    assert consume(ledger, 'support', later, 'support_received').value == 'COMMITTED'
    audit = next(e for e in ledger.events() if e.payload.get('canonical_delivery_id') == 'support')
    saved_audit = dict(audit.payload)
    row = {'private_world_delivery_id': 'support'}
    observe(row, ledger.snapshot(), ledger.events())
    assert row['contact_qualification'] is True
    revision = row['contact_projection_revision']
    canonical(ledger, 'earlier-conflict', NOW)
    assert consume(ledger, 'earlier-conflict', NOW, 'conflict').value == 'COMMITTED'
    ledger = SQLitePrivateWorldLedger(path)
    current = next(e for e in ledger.events() if e.event_id == audit.event_id)
    assert current.payload == saved_audit
    assert current.projection_result['contact_qualification'] is False
    assert ledger.projection_result(audit.event_id) == current.projection_result
    observe(row, ledger.snapshot(), ledger.events())
    assert row['contact_qualification'] is False
    assert row['contact_projection_revision'] > revision


def test_checkpoint_cooldown_uses_projected_contribution_not_receipt_audit(tmp_path):
    from dataclasses import replace
    ledger = SQLitePrivateWorldLedger(tmp_path / 'cooldown-checkpoint.db')
    signal = {'kind': 'support_received', 'user_quote': 'same', 'reply_quote': 'reply'}
    for identity, at in [('later', NOW + timedelta(minutes=1)), ('earlier', NOW)]:
        canonical(ledger, identity, at)
        assert PrivateWorldRelationshipCommitter(ledger).commit_exchange(
            identity, 'same', 'reply', signal, occurred_at=at).value == 'COMMITTED'
    assert ledger.snapshot().trust == 1
    current = ledger.snapshot()
    ledger.apply_once(LedgerEvent('opaque', 'opaque', 'opaque', {}, (NOW + timedelta(minutes=2)).isoformat()),
        replace(current, version=current.version + 1, nickname_permissions=('legacy-write',)))
    at = NOW + timedelta(hours=24)
    canonical(ledger, 'next-day', at)
    assert PrivateWorldRelationshipCommitter(ledger).commit_exchange(
        'next-day', 'same', 'reply', signal, occurred_at=at).value == 'COMMITTED'
    assert ledger.snapshot().trust == 2

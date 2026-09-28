"""Late canonical consumers retain the existing 24-hour evidence cooldown."""
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from private_world_ledger import SQLitePrivateWorldLedger
from runtime.memory.private_world_delivery import DeliveryEvent, PrivateWorldDeliveryCommitter
from runtime.memory.private_world_relationship import PrivateWorldRelationshipCommitter


NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)
KINDS = ('support_received', 'boundary_respected', 'conflict', 'repair',
         'meaningful_exchange', 'shared_experience')


def setup_pair(tmp_path, kind, gap):
    ledger = SQLitePrivateWorldLedger(tmp_path / 'world.db')
    canonical = PrivateWorldDeliveryCommitter(ledger)
    for identity, at in [('old:1', NOW), ('new:1', NOW + gap)]:
        canonical.commit(DeliveryEvent(delivery_id=identity, occurred_at=at, semantic_key=identity,
            canonical_reply_sha256=hashlib.sha256(b'reply').hexdigest()))
    committer = PrivateWorldRelationshipCommitter(ledger)
    signal = {'kind': kind, 'user_quote': 'user', 'reply_quote': 'reply'}
    assert committer.commit_exchange('new:1', 'user', 'reply', signal, occurred_at=NOW + gap).value == 'COMMITTED'
    return ledger, committer, signal


def scores(ledger):
    snapshot = ledger.snapshot()
    return tuple(getattr(snapshot, key) for key in ('familiarity', 'trust', 'comfort', 'closeness', 'tension', 'growth_used'))


@pytest.mark.parametrize('kind', KINDS)
def test_future_equivalent_within_cooldown_is_committed_without_scoring(tmp_path, kind):
    ledger, committer, signal = setup_pair(tmp_path, kind, timedelta(minutes=1))
    before = scores(ledger)
    assert committer.commit_exchange('old:1', 'user', 'reply', signal, occurred_at=NOW).value == 'COMMITTED'
    assert scores(ledger) == before
    record = next(event for event in ledger.events() if event.payload.get('canonical_delivery_id') == 'old:1')
    # Ordered projection assigns the contribution to the earliest event. A
    # late event can move the growth-window origin without adding any scores;
    # submission-order audit classification is no longer the state authority.
    assert not set(record.payload['change_fields']) - {'growth_window_start'}
    forward = SQLitePrivateWorldLedger(tmp_path / 'forward.db')
    delivery = PrivateWorldDeliveryCommitter(forward)
    ordered = PrivateWorldRelationshipCommitter(forward)
    for identity, at in [('old:1', NOW), ('new:1', NOW + timedelta(minutes=1))]:
        delivery.commit(DeliveryEvent(delivery_id=identity, occurred_at=at, semantic_key=identity,
            canonical_reply_sha256=hashlib.sha256(b'reply').hexdigest()))
        assert ordered.commit_exchange(identity, 'user', 'reply', signal, occurred_at=at).value == 'COMMITTED'
    assert ledger.snapshot().growth_window_start == forward.snapshot().growth_window_start
    assert scores(ledger) == scores(forward)
    count = len(ledger.events())
    assert committer.commit_exchange('old:1', 'user', 'reply', signal, occurred_at=NOW).value == 'DUPLICATE'
    assert len(ledger.events()) == count
    assert scores(ledger) == before


@pytest.mark.parametrize('gap', [timedelta(hours=24), timedelta(hours=25)])
def test_future_equivalent_outside_cooldown_does_not_discard_valid_history(tmp_path, gap):
    ledger, committer, signal = setup_pair(tmp_path, 'conflict', gap)
    before = ledger.snapshot().tension
    assert committer.commit_exchange('old:1', 'user', 'reply', signal, occurred_at=NOW).value == 'COMMITTED'
    assert ledger.snapshot().tension == before + 3
    record = next(event for event in ledger.events() if event.payload.get('canonical_delivery_id') == 'old:1')
    assert record.payload['applied'] is True


def test_future_equivalent_does_not_bypass_canonical_validation(tmp_path):
    ledger, committer, signal = setup_pair(tmp_path, 'support_received', timedelta(minutes=1))
    before = len(ledger.events())
    assert committer.commit_exchange('missing:1', 'user', 'reply', signal, occurred_at=NOW).value == 'REJECTED'
    assert len(ledger.events()) == before

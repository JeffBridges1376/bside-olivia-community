import pytest
from original_client_server import _recent_diagnostic_tasks


def test_recent_qq_failure_is_not_hidden_by_old_unconfirmed_wechat_tasks():
    old = [dict(channel='wechat', letter_status='PROCESSING',
                delivery_status='DELIVERY_UNCONFIRMED', created_at=i)
           for i in range(30)]
    failed = dict(channel='qq', letter_status='FAILED', delivery_status='FAILED',
                  error_code='PERSONAL_CHAT_GENERATION_FAILED', created_at=100)
    snapshot = _recent_diagnostic_tasks([*old, failed])
    assert len(snapshot) == 20
    assert snapshot[0] is failed


@pytest.mark.parametrize('reverse', [False, True])
def test_snapshot_prioritizes_active_tasks_then_newest_completed(reverse):
    rows = [{'letter_status': 'COMPLETED', 'created_at': i} for i in range(50)]
    active = {'letter_status': 'COMPLETED', 'media_status': 'processing', 'created_at': -1}
    rows.append(active)
    if reverse:
        rows.reverse()
    snapshot = _recent_diagnostic_tasks(rows)
    assert len(snapshot) == 20
    assert snapshot[0] is active
    assert [r['created_at'] for r in snapshot[1:]] == list(range(49, 30, -1))


def test_snapshot_ignores_malformed_entries():
    assert _recent_diagnostic_tasks(None) == ()
    assert _recent_diagnostic_tasks([None, 'private', {'created_at': None}]) == ({'created_at': None},)


def test_letters_stay_visible_behind_stuck_chats_and_recent_chat_failures():
    day = 86_400
    now = 30 * day
    stuck = [dict(channel='wechat', letter_status='PROCESSING', delivery_status='DELIVERY_UNCONFIRMED',
                  created_at=now - 7 * day + i) for i in range(13)]
    failures = [dict(channel='qq', letter_status='FAILED', delivery_status='FAILED',
                     created_at=now - 3600 + i) for i in range(15)]
    letters = [dict(letter_id=f'l{i}', letter_status='COMPLETED', created_at=now - 600 + i) for i in range(10)]
    snapshot = _recent_diagnostic_tasks([*stuck, *failures, *letters])
    assert len(snapshot) == 20
    assert not any(item in stuck for item in snapshot)
    kept = [item for item in snapshot if item.get('letter_id')]
    assert [item['letter_id'] for item in kept] == [f'l{i}' for i in range(9, 1, -1)]
    assert sum(item in failures for item in snapshot) == 12

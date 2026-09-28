from datetime import datetime, timezone

from runtime.reply.character_emotion_context import (
    freeze_expression_context, latest_qq_reply_basis, store_expression_context,
)


def row(stamp='2026-09-28T08:40:00+00:00', *, channel='qq'):
    value = dict(channel=channel, delivery_status='DELIVERED',
                 private_world_occurred_at=stamp, reply_text='慢慢来。')
    snapshot = freeze_expression_context('synthetic', datetime(2026, 9, 28, 8, tzinfo=timezone.utc),
        world={'current': {'activity': '练琴', 'source_id': 'private-source'}, 'stale': False},
        emotion={'reactions': [{'reaction': 'relieved', 'quote': '终于弹顺了',
                               'goal_or_need': '把曲子弹稳', 'source_id': 'private-source'}],
                 'concerns': [{'summary': '明天的展示'}]})
    store_expression_context(value, snapshot, value['reply_text'])
    return value


def test_latest_confirmed_qq_basis_uses_bound_snapshot_and_omits_private_ids():
    first = row()
    future = row('2026-09-28T09:00:00+00:00')
    future['delivery_status'] = 'SENDING'
    other = row('2026-09-28T10:00:00+00:00', channel='wechat')
    basis = latest_qq_reply_basis([first, future, other])
    assert basis['activity'] == '练琴'
    assert basis['as_of'] == '2026-09-28T08:00:00+00:00'
    assert basis['reactions'][0]['reaction'] == 'relieved'
    assert 'private-source' not in str(basis)
    assert 'reply_text' not in basis


def test_latest_missing_or_changed_body_does_not_reuse_older_reply_basis():
    first, second = row(), row('2026-09-28T09:00:00+00:00')
    second['reply_text'] = '另一份正文'
    assert latest_qq_reply_basis([first, second]) == {'status': 'missing'}
    second.pop('expression_context')
    assert latest_qq_reply_basis([first, second]) == {'status': 'missing'}
    assert latest_qq_reply_basis([]) == {'status': 'missing'}

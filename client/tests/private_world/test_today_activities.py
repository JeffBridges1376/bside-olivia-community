from datetime import datetime, timedelta, timezone

from runtime.private_world.daily_life import DailyLifeStore


def test_today_activities_use_beijing_day_and_only_published_past_non_meals(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.db')
    now = datetime(2026, 9, 28, 8, tzinfo=timezone.utc)
    stamps = [('old', now - timedelta(hours=17), 'rest'),
              ('early', now - timedelta(hours=15), 'reading'),
              ('meal', now - timedelta(hours=4), 'meal'),
              ('later', now - timedelta(hours=1), 'practice'),
              ('future', now + timedelta(hours=1), 'rest')]
    for identity, stamp, kind in reversed(stamps):
        store.publish_day(identity, dict(location='家里', activity=identity, note=identity), [],
                          occurred_at=stamp, activity_kind=kind)
    rows = store.snapshot(now)['world']['today_activities']
    assert [row['source_id'] for row in rows] == ['early', 'later']
    assert rows[0] == dict(source_id='early', occurred_at='2026-09-27T17:00:00+00:00',
                          activity='early', activity_kind='reading', note='early')

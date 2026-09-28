from datetime import datetime, timedelta, timezone

import pytest

from runtime.private_world.daily_life import DailyLifeStore


@pytest.mark.parametrize('hour', [8, 12, 18])
def test_mealtime_reopens_life_decision_without_fabricating_food(tmp_path, hour):
    boundary = datetime(2026, 9, 27, hour, tzinfo=timezone(timedelta(hours=8)))
    store = DailyLifeStore(tmp_path/'life.db')
    store.publish_day('day:rest', {'location': '家里', 'activity': '休息', 'note': '在家休息。'}, [],
                      occurred_at=boundary-timedelta(minutes=20), activity_kind='rest')
    assert not store.snapshot(boundary-timedelta(seconds=1))['stale']
    state = store.snapshot(boundary)
    assert state['stale']
    assert state['world']['meals'] == []
    # After a fresh decision, repeated reads do not force repeated generation.
    store.publish_day('day:next', {'location': '家里', 'activity': '休息', 'note': '继续休息。'}, [],
                      occurred_at=boundary, activity_kind='rest')
    assert not store.snapshot(boundary+timedelta(minutes=1))['stale']

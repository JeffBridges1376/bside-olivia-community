"""Background life must not pay for judgments that change nothing.

Every reconsideration of Linli's day is a paid judgment. These tests pin the
three controls: repeating the same activity backs off, an absent user slows
life to 3-5 hours, and a repeated activity skips the episode and development
steps (rest keeps its episode, the only evidence for recovering energy).
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from runtime.private_world import daily_life, daily_life_runtime as runtime_module
from runtime.private_world.daily_life import DailyLifeStore, _activity_refresh_delay
from runtime.private_world.daily_life_runtime import DailyLifeRuntime

AFTERNOON = datetime(2026, 9, 5, 7, tzinfo=timezone.utc)  # Saturday 15:00 Shanghai, no class.


@pytest.mark.parametrize('repeats,minutes', [(0, 60), (1, 120), (2, 240), (3, 240), (8, 240)])
def test_repeated_activity_backs_off_to_four_hours(repeats, minutes):
    current = {'activity_kind': 'rest', 'source_id': 'day:x'}
    assert _activity_refresh_delay(current, repeats=repeats) == timedelta(minutes=minutes)


def test_rest_no_longer_expires_every_half_hour():
    assert _activity_refresh_delay({'activity_kind': 'rest', 'source_id': 'day:x'}) == timedelta(minutes=60)


def test_absent_user_slows_every_activity_to_three_to_five_hours():
    for kind in ('rest', 'practice', 'walk', 'meal', None):
        delay = _activity_refresh_delay({'activity_kind': kind, 'source_id': 'day:x'}, idle=True)
        assert timedelta(hours=3) <= delay <= timedelta(hours=5)


def publish(store, source, kind, when):
    store.publish_day(source, {'location': '家里', 'activity': kind, 'note': '在家里。'}, [],
                      occurred_at=when, activity_kind=kind)


def test_snapshot_counts_consecutive_same_activity(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.db')
    publish(store, 'day:a', 'practice', AFTERNOON - timedelta(hours=4, minutes=30))
    publish(store, 'day:b', 'rest', AFTERNOON - timedelta(hours=4))
    publish(store, 'day:c', 'rest', AFTERNOON - timedelta(hours=3))
    last = AFTERNOON - timedelta(hours=2)  # 13:00; four hours later stays before the 18:00 meal.
    publish(store, 'day:d', 'rest', last)
    # Third rest in a row: 60 min doubled twice.
    assert not store.snapshot(last + timedelta(minutes=239))['stale']
    assert store.snapshot(last + timedelta(minutes=240))['stale']


@pytest.mark.real_user_idle
def test_life_waits_hours_while_the_user_is_away_and_resumes_when_they_write(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.db')
    publish(store, 'day:a', 'practice', AFTERNOON)
    later = AFTERNOON + timedelta(minutes=90)
    assert not store.snapshot(later)['stale']  # Nobody has written: 3-5 hours.
    with store._db() as db:
        db.execute('INSERT INTO life_rest_exchanges VALUES (?,?,?)',
                   ('reply:u:1', (later - timedelta(minutes=5)).isoformat(), later.isoformat()))
    assert store.snapshot(later)['stale']  # Active again: the 60-minute cadence applies.


class Duty:
    def __init__(self):
        self.calls = []

    async def evaluate(self, kind, packet):
        self.calls.append(kind)
        from runtime.private_world.character_development import digest
        return SimpleNamespace(decision={'candidates': []}, error_code=None, input_digest=digest(packet))


def world(tmp_path, monkeypatch, previous_kind, decided_kind):
    duty, episodes = Duty(), []
    monkeypatch.setattr(runtime_module, 'configured_duties', lambda: duty, raising=False)
    import runtime.reply.jev_questions as jev_questions
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: object())
    import runtime.private_world.meal_lifecycle as meal_lifecycle
    import runtime.private_world.life_episode as life_episode
    async def no_meals(*args, **kwargs):
        return None
    async def episode(port, source_id, now, kind, context, **kwargs):
        episodes.append(kind)
        raise ValueError('LIFE_EPISODE_INVALID')  # Only the call matters here.
    monkeypatch.setattr(meal_lifecycle, 'advance', no_meals)
    monkeypatch.setattr(life_episode, 'create', episode)
    store = DailyLifeStore(tmp_path / 'life.db')
    publish(store, 'day:prev', previous_kind, AFTERNOON - timedelta(hours=5))
    runtime = DailyLifeRuntime(store, lambda: None, lambda: '[]')
    async def complete(prompt, data, request_id, **kwargs):
        return deepcopy({'activity': {'kind': decided_kind, 'place_id': 'home', 'focus': ''},
                         'meal': None, 'project': None})
    runtime._complete = complete
    asyncio.run(runtime.refresh(AFTERNOON))
    return store, runtime, duty, episodes


def test_repeated_activity_skips_episode_and_development(tmp_path, monkeypatch):
    store, runtime, duty, episodes = world(tmp_path, monkeypatch, 'practice', 'practice')
    assert runtime.error_code is None
    assert episodes == [] and duty.calls == []
    assert store.snapshot(AFTERNOON)['current']['source_id'] != 'day:prev'


def test_changed_activity_still_runs_the_episode(tmp_path, monkeypatch):
    _, _, _, episodes = world(tmp_path, monkeypatch, 'reading', 'practice')
    assert episodes == ['practice']


def test_repeated_rest_keeps_its_episode_for_recovery(tmp_path, monkeypatch):
    _, _, duty, episodes = world(tmp_path, monkeypatch, 'rest', 'rest')
    assert episodes == ['rest'] and duty.calls == []

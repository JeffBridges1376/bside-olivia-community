"""Severe existing care must not loosen the authored world's activity policy."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime
from runtime.private_world.world_decision import compile_decision, decision_context


def strained_store(path, now):
    store = DailyLifeStore(path)
    for index in (1, 2, 3):
        start = now.replace(hour=16, minute=0) - timedelta(days=index)
        store.record_exchange(f'reply:strain:{index}', 'synthetic', 'synthetic', [],
                              received_at=start, occurred_at=start + timedelta(hours=5))
    return store


def activity(kind):
    return {'activity': {'kind': kind, 'place_id': 'campus' if kind == 'class' else 'home',
                         'focus': ''}, 'meal': None, 'project': None}


@pytest.mark.parametrize('hour', [6, 11])  # Shanghai classroom and evening free time.
def test_consultation_care_keeps_rest_constraint_from_real_rhythm(tmp_path, hour):
    now = datetime(2026, 9, 28, hour, 15, tzinfo=timezone.utc)
    store = strained_store(tmp_path / 'world.db', now)
    state = store.snapshot(now)
    assert state['rhythm']['wellbeing']['care'] == 'consider_consultation'
    data = decision_context({'time': now.isoformat(), 'persona': '[]',
        'world': state['world'], 'rhythm': state['rhythm'], 'projects': []})
    assert data['allowed_activity_kinds'] == ['rest']
    for kind in ('class', 'practice', 'creative'):
        with pytest.raises(ValueError, match='CLASS_CONFLICT'):
            compile_decision(activity(kind), data)
    current, _, _ = compile_decision(activity('rest'), data)
    assert current['activity'] == '休息'


def test_background_refresh_corrects_severe_care_activity_before_publication(tmp_path):
    now = datetime(2026, 9, 28, 6, 15, tzinfo=timezone.utc)
    store = strained_store(tmp_path / 'world.db', now)
    requests = []

    class Gateway:
        async def complete(self, messages, **kwargs):
            requests.append(json.loads(messages[-1]['content']))
            assert store.snapshot(now)['current'] is None
            return SimpleNamespace(text=json.dumps(activity('class' if len(requests) == 1 else 'rest')))

    runtime = DailyLifeRuntime(store, lambda: Gateway(), lambda: '[]')
    asyncio.run(runtime.refresh(now))
    assert len(requests) == 2
    assert requests[0]['allowed_activity_kinds'] == ['rest']
    assert requests[1]['validation_error'] == 'DAILY_LIFE_DECISION_CLASS_CONFLICT'
    assert runtime.error_code is None
    assert store.snapshot(now)['current']['activity'] == '休息'

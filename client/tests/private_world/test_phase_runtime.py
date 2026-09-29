"""Acceptance at the real persona -> autonomous world -> reply context boundary."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime, life_persona


NOW = datetime(2026, 9, 27, 6, tzinfo=timezone.utc)
PERSONA = Path(__file__).resolve().parents[2] / 'linli_character/persona_release_v2.json'


def test_phase_seed_is_available_to_world_then_real_progress_replaces_initial_plan(tmp_path):
    life = DailyLifeStore(tmp_path / 'life.sqlite')
    calls = []
    runtime = DailyLifeRuntime(life, lambda: None, lambda: life_persona(PERSONA))

    async def no_emotion(_):
        pass

    async def complete(prompt, data, request_id, **_):
        calls.append(json.loads(json.dumps(data)))
        project = next(p for p in data['projects'] if p['id'] == 'phase:anchor.current_piece')
        assert project['evidence_kind'] == 'initial_plan'
        assert project['status'] == 'planned'
        return {'activity': {'kind': 'practice', 'place_id': 'home', 'focus': '夜曲音色'}, 'meal': None, 'development': [],
                'project': {'id': project['id'], 'title': project['title'], 'status': 'ongoing',
                            'progress': '开始比较两种触键方式，尚未决定。', 'next_activity': None}}
    runtime._complete = complete
    asyncio.run(runtime.refresh(NOW))
    assert runtime.error_code is None
    assert len(calls) == 1
    assert calls[0]['previous'] is None
    assert calls[0]['recent_life'] == []
    assert life.exchange_state(now=NOW - timedelta(microseconds=1))['projects'] == []
    result = json.loads(life.reply_context('夜曲练得怎么样', now=NOW, max_chars=10000))
    item = next(p for p in result['threads'] if p['id'] == 'phase:anchor.current_piece')
    assert item['evidence_kind'] == 'published_life'
    assert item['status'] == 'ongoing'
    assert '尚未决定' in item['detail']
    assert item['source_id'].startswith('day:')
    history = life.exchange_state(now=NOW, include_history=True)['projects']
    project = next(p for p in history if p['id'] == item['id'])
    assert [p['evidence_kind'] for p in project['history']] == ['initial_plan', 'published_life']
    later = life.exchange_state(now=datetime(2027, 2, 2, 6, tzinfo=timezone.utc), include_history=True)
    continued = next(p for p in later['projects'] if p['id'] == item['id'])
    assert continued['status'] == 'ongoing'
    assert continued['history'][0]['status'] == 'planned'
    assert not continued['history'][0].get('phase_expired')


def test_unstarted_semester_plan_expires_in_reply_context_without_success_story(tmp_path):
    from runtime.private_world.phase_settings import ensure_phase_projects

    life = DailyLifeStore(tmp_path / 'life.sqlite')
    assert ensure_phase_projects(life, life_persona(PERSONA), NOW) == 2
    later = datetime(2027, 2, 2, 6, tzinfo=timezone.utc)
    view = json.loads(life.reply_context('夜曲练习完成了吗', now=later, max_chars=10000))
    project = next(p for p in view['threads'] if p['id'] == 'phase:anchor.current_piece')
    assert project['status'] == 'paused'
    assert project['evidence_kind'] == 'initial_plan'
    assert project['phase_expired'] is True
    assert view['current'] is None
    assert '没有完成记录' in project['detail']

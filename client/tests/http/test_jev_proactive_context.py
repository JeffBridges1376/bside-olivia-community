import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from runtime.personal_chat.initiative_profile import profile_from_snapshot
from runtime.reply.proactive_runtime import contact_gates, contact_slot, packet, world_gates


NOW = datetime(2026, 9, 27, 11, tzinfo=timezone.utc)
PROFILE = profile_from_snapshot(SimpleNamespace(relationship_stage='committed', tension=0))


def test_world_gates_consider_current_class_and_actual_activity():
    world = {'stale': False, 'rhythm': {'phase': 'free', 'availability': 'open'},
             'world': {'schedule': {'current_class': {'title': '钢琴'}}},
             'current': {'activity_kind': 'reading'}}
    assert world_gates(world) == ['class']
    world['world']['schedule']['current_class'] = None
    world['current']['activity_kind'] = 'practice'
    assert world_gates(world) == ['busy']
    world['current']['activity_kind'] = 'reading'
    world['rhythm'] = {'phase': 'focus', 'availability': 'busy'}
    assert world_gates(world) == []  # Published leisure overrides a generic focus window.
    world['stale'] = True
    assert world_gates(world) == ['busy']
    world['rhythm'] = {'phase': 'sleep', 'availability': 'rest'}
    assert world_gates(world) == ['sleeping']


def test_pause_and_unconfirmed_delivery_apply_across_channels():
    rows = [{'channel': 'qq', 'origin': 'user', 'delivery_status': 'DELIVERED',
             'initiative_preference': 'pause', 'created_at': NOW.timestamp() - 10000}]
    assert contact_gates(rows, now=NOW, profile=PROFILE, channel='letter')['paused']
    rows.append({'channel': 'wechat', 'delivery_status': 'DELIVERY_UNCONFIRMED',
                 'created_at': NOW.timestamp() - 100})
    assert 'pending_reply' in contact_gates(rows, now=NOW, profile=PROFILE, channel='letter')['blocked_reasons']
    rows[0]['pause_until'] = NOW.timestamp() - 1
    assert not contact_gates(rows, now=NOW, profile=PROFILE, channel='qq')['paused']


def test_contact_slot_excludes_other_channel_and_releases_on_failure():
    server = SimpleNamespace()
    with pytest.raises(ValueError):
        with contact_slot(server, 'letter') as acquired:
            assert acquired
            with contact_slot(server, 'qq') as other:
                assert not other
            raise ValueError('synthetic')
    with contact_slot(server, 'qq') as acquired:
        assert acquired


def test_packet_keeps_current_state_without_whole_world_or_wakeup_as_user_input():
    world = {'kind': 'character_life_reference', 'current': {'note': '在看书。'},
             'today_activities':[{'note':'OLD_UNRELATED'}]*50,
             'projects':[{'id':'old','detail':'OLD_UNRELATED'}]}
    value = packet(channel='qq', now=NOW, profile=PROFILE, world=world,
        rhythm={'phase': 'free', 'availability': 'open'}, emotion={},
        messages=[{'role': 'user', 'content': '应用主动检查，不是用户原话'}],
        opportunities=[], rows=[], available_media=['text'],
        hard_gates={'paused': False, 'blocked_reasons': []})
    assert value['recent_dialogue'] == []
    assert value['world']['kind'] == world['kind']
    assert 'OLD_UNRELATED' not in str(value)
    assert value['contact']['seconds_since_last_contact'] is None
    assert value['activity'] == world['current']


def test_proactive_packet_keeps_only_opportunity_referenced_project():
    import json
    value = packet(channel='qq', now=NOW, profile=PROFILE,
        world={'shared':[{'id':'chosen','title':'练习曲子','quote':'一起练这首。','history':['OLD']},
                         {'id':'other','title':'UNRELATED'}]}, rhythm={}, emotion={}, messages=[],
        opportunities=[{'id':'o1','kind':'shared_topic','description':json.dumps({'project_id':'chosen'})}],
        rows=[],available_media=['text'],hard_gates={'paused':False,'blocked_reasons':[]})
    assert value['world']['shared']==[{'id':'chosen','title':'练习曲子','quote':'一起练这首。'}]
    assert 'UNRELATED' not in str(value) and 'OLD' not in str(value)


def test_cross_channel_cooldown_prevents_immediate_second_contact():
    rows = [{'channel': 'qq', 'origin': 'proactive', 'delivery_status': 'DELIVERED',
             'delivered_at': NOW.timestamp() - 30, 'created_at': NOW.timestamp() - 100}]
    assert 'cooldown' in contact_gates(rows, now=NOW, profile=PROFILE, channel='letter')['blocked_reasons']


def test_late_delivered_older_resume_cannot_cancel_newer_pause():
    rows = [dict(letter_id='resume', channel='qq', origin='user', delivery_status='DELIVERED',
                 initiative_preference='open', created_at=NOW.timestamp()-60000, delivered_at=NOW.timestamp()-30000),
            dict(letter_id='pause', channel='wechat', origin='user', delivery_status='DELIVERED',
                 initiative_preference='pause', created_at=NOW.timestamp()-50000, delivered_at=NOW.timestamp()-40000)]
    assert contact_gates(rows, now=NOW, profile=PROFILE, channel='letter')['paused']


def test_old_failed_proactive_attempt_does_not_disable_future_initiative():
    rows = [dict(letter_id='failed', channel='qq', origin='proactive', delivery_status='FAILED',
                 letter_status='FAILED', created_at=NOW.timestamp()-40000)]
    assert contact_gates(rows, now=NOW, profile=PROFILE, channel='letter')['blocked_reasons'] == []

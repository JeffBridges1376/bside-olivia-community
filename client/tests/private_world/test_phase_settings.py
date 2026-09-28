"""A migrated semester setting is an initial plan, never a past event."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.phase_settings import ensure_phase_projects, project_at


NOW = datetime(2026, 9, 27, 6, tzinfo=timezone.utc)


def persona(**changes):
    item = {"declaration_id": "anchor.current_piece", "inclusion": "phase",
            "phase_seed": {"title": "肖邦夜曲练习", "detail": "初始练习安排：关注音色。",
                           "activity_kind": "practice", "scope": "activation_semester"}}
    item.update(changes)
    return json.dumps([item], ensure_ascii=False)


def recorded(store):
    with store._db() as db:
        return [json.loads(row[0]) for row in db.execute("SELECT payload FROM life_projects")]


def test_seed_is_durable_planned_and_does_not_claim_current_activity(tmp_path):
    store = DailyLifeStore(tmp_path / "life.sqlite")
    assert ensure_phase_projects(store, persona(), NOW) == 1
    item, = recorded(store)
    assert item["status"] == "planned"
    assert item["evidence_kind"] == "initial_plan"
    assert item["updated_at"] == NOW.isoformat()
    assert "老师" not in item["detail"]
    assert store.snapshot(NOW)["current"] is None
    assert store.recent_life(NOW) == []
    reopened = DailyLifeStore(store.path)
    assert ensure_phase_projects(reopened, persona(), NOW + timedelta(days=10)) == 0
    assert recorded(reopened) == [item]


def test_expiry_is_unfinished_plan_not_completed_or_recurring_next_semester(tmp_path):
    store = DailyLifeStore(tmp_path / "life.sqlite")
    ensure_phase_projects(store, persona(), NOW)
    item, = recorded(store)
    end = datetime.fromisoformat(item["valid_until"])
    assert project_at(item, end - timedelta(seconds=1))["status"] == "planned"
    expired = project_at(item, end)
    assert expired["status"] == "paused"
    assert expired["phase_expired"] is True
    assert "待重新安排" in expired["detail"]
    assert item["status"] == "planned"  # Projection never mutates history.
    assert ensure_phase_projects(store, persona(), end + timedelta(days=30)) == 0
    progressed = {"id": item["id"], "status": "ongoing", "evidence_kind": "published_life"}
    assert project_at(progressed, end) == progressed


@pytest.mark.parametrize("when", [datetime(2025, 8, 10, tzinfo=timezone.utc),
                                  datetime(2029, 7, 2, tzinfo=timezone.utc)])
def test_student_phase_not_seeded_before_enrollment_or_after_graduation(tmp_path, when):
    store = DailyLifeStore(tmp_path / "life.sqlite")
    assert ensure_phase_projects(store, persona(), when) == 0
    assert recorded(store) == []


def test_migration_does_not_overwrite_existing_project_or_duplicate_same_title(tmp_path):
    store = DailyLifeStore(tmp_path / "life.sqlite")
    store.publish_day("daily:old", {"location": "家里", "activity": "练琴", "note": "练习结束。"},
                      [{"id": "my-practice", "title": "肖邦夜曲练习", "detail": "录音已完成。", "status": "completed"}],
                      occurred_at=NOW - timedelta(days=1))
    before = recorded(store)
    assert ensure_phase_projects(store, persona(), NOW) == 0
    assert recorded(store) == before


def test_contextual_or_unstructured_static_text_cannot_become_a_world_event(tmp_path):
    store = DailyLifeStore(tmp_path / "life.sqlite")
    assert ensure_phase_projects(store, persona(inclusion="contextual"), NOW) == 0
    assert ensure_phase_projects(store, persona(phase_seed=None), NOW) == 0
    assert recorded(store) == []


def test_unknown_or_oversized_phase_schema_is_rejected_without_partial_write(tmp_path):
    store = DailyLifeStore(tmp_path / "life.sqlite")
    value = json.loads(persona())
    value.append({**value[0], "declaration_id": "other", "phase_seed": {**value[0]["phase_seed"], "extra": "ignore"}})
    with pytest.raises(ValueError, match="PHASE_SETTING_INVALID"):
        ensure_phase_projects(store, json.dumps(value), NOW)
    assert recorded(store) == []

"""Observation expiry does not establish activity or meal completion."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime, _REFRESH_RETRY_INITIAL
from tests.private_world.decisions import life_decision


NOON = datetime(2026, 9, 27, 4, tzinfo=timezone.utc)  # Sunday 12:00 in Shanghai.
CURRENT = {"location": "家里", "activity": "正在吃午饭", "note": "正在吃午饭：番茄鸡蛋饭。"}
MEAL = {"slot": "lunch", "food": "番茄鸡蛋饭", "status": "eating"}


def stored_moments(store):
    with store._db() as db:
        return [tuple(row) for row in db.execute("SELECT * FROM life_moments ORDER BY source_id")]


@pytest.mark.parametrize("status", ["planned", "eating"])
def test_legacy_meal_expires_without_completing_or_rewriting_it_across_restart(tmp_path, status):
    store = DailyLifeStore(tmp_path / "life.db")
    store.publish_day("day:lunch", CURRENT, [], occurred_at=NOON, meals=[{**MEAL, "status": status}])
    original = stored_moments(store)
    expiry = NOON + timedelta(hours=1)
    assert store.snapshot(expiry - timedelta(microseconds=1))["stale"] is False
    assert store.snapshot(expiry)["stale"] is True
    afternoon = NOON + timedelta(hours=2, minutes=30)
    restarted = DailyLifeStore(store.path)
    state = restarted.snapshot(afternoon)
    assert state["stale"] is True
    meal = state["world"]["meals"][0]
    assert meal["status"] == status and meal["food"] == MEAL["food"]
    assert meal["stale"] is True and datetime.fromisoformat(meal["valid_until"]) == expiry
    context = json.loads(restarted.reply_context("你现在做什么？", now=afternoon))
    assert context["current"] is None and context["last_observation"]["source_id"] == "day:lunch"
    assert context["meals"][0]["stale"] is True and context["meals"][0]["status"] == status
    assert stored_moments(restarted) == original


@pytest.mark.parametrize("kind,minutes", [("meal", 60), ("walk", 60), ("errand", 60), ("housework", 60), ("practice", 60)])
def test_explicit_short_activity_kind_survives_restart_and_expires_at_its_boundary(tmp_path, kind, minutes):
    store = DailyLifeStore(tmp_path / "life.db")
    # Text alone is deliberately uninformative; the verified enum drives expiry.
    store.publish_day("day:short", {"location": "家里", "activity": "日常活动", "note": "一段生活。"}, [],
                      occurred_at=NOON, activity_kind=kind, meals=[MEAL] if kind == "meal" else [])
    restarted = DailyLifeStore(store.path)
    expiry = NOON + timedelta(minutes=minutes)
    assert restarted.snapshot(NOON)["current"]["activity_kind"] == kind
    assert not restarted.snapshot(expiry - timedelta(microseconds=1))["stale"]
    assert restarted.snapshot(expiry)["stale"]


def test_untyped_prose_is_not_classified_and_keeps_old_lifetime(tmp_path):
    store = DailyLifeStore(tmp_path / 'legacy.db')
    store.publish_day("day:long", {"location": "家里", "activity": "散步", "note": "随手记下一段生活。"}, [],
                      occurred_at=NOON, activity_kind=None)
    assert not store.snapshot(NOON + timedelta(hours=2, minutes=30))["stale"]
    assert store.snapshot(NOON + timedelta(hours=5, seconds=1))["stale"]
    for invalid in ("walking", "anything", 42, [], {}):
        with pytest.raises(ValueError, match="DAILY_LIFE_ACTIVITY_KIND_INVALID"):
            store.publish_day("day:invalid", CURRENT, [], occurred_at=NOON, activity_kind=invalid)
    assert not store.has_source("day:invalid")


def test_short_meal_refresh_failure_stays_stale_and_retry_recovers_without_inventing_completion(tmp_path):
    store = DailyLifeStore(tmp_path / "life.db")

    class Gateway:
        calls = []

        async def complete(self, messages, **_kwargs):
            self.calls.append(json.loads(messages[1]["content"]))
            if len(self.calls) == 1:
                result = life_decision(messages, kind="meal", meal=MEAL)
            elif len(self.calls) == 2:
                raise RuntimeError("synthetic unavailable provider")
            else:
                result = life_decision(messages, kind="walk", focus="街边的树")
            return SimpleNamespace(text=json.dumps(result, ensure_ascii=False))

    gateway = Gateway()
    runtime = DailyLifeRuntime(store, lambda: gateway, lambda: "大学生活。")

    async def run():
        await runtime.refresh(NOON)
        assert store.snapshot(NOON)["current"]["activity_kind"] == "meal"
        original = stored_moments(store)
        await runtime.refresh(NOON + timedelta(minutes=59))
        assert len(gateway.calls) == 1
        expiry = NOON + timedelta(hours=1)
        await runtime.refresh(expiry)
        assert len(gateway.calls) == 2 and runtime.error_code == "DAILY_LIFE_GENERATION_UNAVAILABLE"
        assert store.snapshot(expiry)["stale"] is True
        assert stored_moments(store) == original
        restarted = DailyLifeRuntime(DailyLifeStore(store.path), lambda: gateway, lambda: "大学生活。")
        await restarted.refresh(expiry + timedelta(minutes=1))
        assert len(gateway.calls) == 2
        recovered_at = expiry + _REFRESH_RETRY_INITIAL
        await restarted.refresh(recovered_at)
        assert len(gateway.calls) == 3 and restarted.error_code is None
        state = restarted.snapshot(recovered_at)
        assert not state["stale"] and state["current"]["activity_kind"] == "walk"
        assert state["world"]["meals"][0]["status"] == "eating"
        assert state["world"]["meals"][0]["stale"] is True
        assert gateway.calls[-1]["world"]["meals"][0]["stale"] is True
        assert original[0] in stored_moments(store)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["text_letter", "future_im"])
def test_expired_meal_reaches_real_generation_preparation_as_past_not_current(tmp_path, mode):
    import re
    from local_server import LetterAdapter
    from llm_gateway import GatewayConfig
    from memory_port import NullMemoryPort
    from reply_context import ReplyMode
    from reply_orchestrator import ReplyRequest
    from runtime.reply.reply_pipeline import _prepare_generation_request

    store = DailyLifeStore(tmp_path / "life.db")
    store.publish_day("day:lunch", CURRENT, [], occurred_at=NOON, meals=[MEAL])
    runtime = DailyLifeRuntime(store, lambda: None, lambda: "")
    adapter = LetterAdapter(GatewayConfig(provider="mock"), memory_port=NullMemoryPort(), daily_life=runtime)
    adapter._now = lambda: NOON + timedelta(hours=2, minutes=30)
    context = adapter.build_reply_context(ReplyMode(mode), future_im_enabled=True)
    prepared = _prepare_generation_request(ReplyRequest(content="你现在做什么？"), context,
        SimpleNamespace(gateway=SimpleNamespace(adapter=adapter)))
    wrappers = [json.loads(match) for message in prepared.request.messages if message["role"] == "system"
                for match in re.findall(r"<evidence_summary>\s*(.*?)\s*</evidence_summary>", message["content"], re.DOTALL)]
    life = next(json.loads(wrapper["text"]) for wrapper in wrappers if wrapper.get("fragment_id") == "linli.daily-life")
    assert life["current"] is None and life["last_observation"]["source_id"] == "day:lunch"
    assert life["meals"][0]["stale"] is True and life["meals"][0]["status"] == "eating"
    rhythm = next(json.loads(wrapper["text"]) for wrapper in wrappers if wrapper.get("fragment_id") == "linli.rhythm")
    assert rhythm["phase"] == "focus"


@pytest.mark.parametrize("status", ["eaten", "skipped"])
def test_recorded_meal_outcome_remains_a_fact_after_activity_observation_expires(tmp_path, status):
    store = DailyLifeStore(tmp_path / "life.db")
    store.publish_day("day:outcome", CURRENT, [], occurred_at=NOON, activity_kind="meal",
                      meals=[{**MEAL, "status": status}])
    state = store.snapshot(NOON + timedelta(hours=2, minutes=30))
    assert state["stale"] is True
    meal = state["world"]["meals"][0]
    assert meal["status"] == status and meal["stale"] is False and meal["valid_until"] is None

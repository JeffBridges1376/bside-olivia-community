from datetime import datetime, timedelta, timezone
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from tests.private_world.decisions import life_decision

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import (
    DailyLifeRuntime,
    _REFRESH_FAILURE_LIMIT,
    _REFRESH_RETRY_INITIAL,
    _REFRESH_RETRY_MAX,
)


NOW = datetime(2026, 9, 7, 17, tzinfo=timezone.utc)


def _source_id(now: datetime) -> str:
    local = now.astimezone(timezone(timedelta(hours=8)))
    return f"day:{local:%Y%m%d}:{local.hour // 6}"


def _retry_row(store: DailyLifeStore, source_id: str):
    with store._db() as db:
        return db.execute(
            "SELECT failure_count, retry_after FROM daily_life_refresh_retry WHERE source_id=?",
            (source_id,),
        ).fetchone()


class FailingThenSuccessfulGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("synthetic provider failure")
        return SimpleNamespace(text=json.dumps(life_decision(_messages)))


class AlwaysFailingGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages, **_kwargs):
        self.calls += 1
        raise RuntimeError("synthetic provider failure")


class ClassifiedProviderFailure(RuntimeError):
    def __init__(self, code: str = "", status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(code or str(status))


class HardFailingGateway:
    def __init__(self, code: str = "", status: int | None = None) -> None:
        self.calls = 0
        self.code = code
        self.status = status

    async def complete(self, _messages, **_kwargs):
        self.calls += 1
        raise ClassifiedProviderFailure(self.code, self.status)


@pytest.mark.parametrize('pending', [False, True])
def test_explicit_refresh_rechecks_pending_times_without_bypassing_backoff(tmp_path, pending):
    now = datetime(2026, 9, 28, 8, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path / 'world.sqlite3')
    if pending:
        quote = '明天早上七点前起床。'
        store.record_exchange('reply:early:1', '早点休息。', quote, [dict(
            id='early', title='早起', detail=quote, quote=quote, status='planned',
            kind='linli', actor='linli')], occurred_at=now - timedelta(hours=12))
    store.publish_day('day:fresh', dict(location='家里', activity='休息', note='正在休息。'),
                      [], occurred_at=now, activity_kind='rest')
    gateway = AlwaysFailingGateway()
    runtime = DailyLifeRuntime(store, lambda: gateway, lambda: '喜欢音乐。')

    async def run():
        await runtime.refresh(now + timedelta(minutes=1))
        assert gateway.calls == 0
        runtime.schedule_refresh(now + timedelta(minutes=2), recheck_projects=True)
        await runtime._task
        assert gateway.calls == int(pending)
        # Manual refresh never resets the existing provider failure backoff.
        await runtime.refresh(now + timedelta(minutes=3), recheck_projects=True)
        assert gateway.calls == int(pending)

    asyncio.run(run())


def test_running_client_refreshes_stale_world_without_opening_world_page(tmp_path, monkeypatch):
    import local_server as server

    store = DailyLifeStore(tmp_path / "life.sqlite3")
    refreshed = asyncio.Event()

    class Gateway:
        async def complete(self, _messages, **_kwargs):
            refreshed.set()
            return SimpleNamespace(text=json.dumps(life_decision(_messages, kind='practice', focus='一小段曲子')))

    runtime = DailyLifeRuntime(store, Gateway, lambda: "林离喜欢弹琴。")
    monkeypatch.setattr(server, "daily_life_runtime", runtime)
    monkeypatch.setattr(server, "_DAILY_LIFE_REFRESH_CHECK_SECONDS", 0.01)

    async def run():
        task = asyncio.create_task(server._refresh_daily_life_periodically())
        try:
            await asyncio.wait_for(refreshed.wait(), 1)
            await runtime._task
            assert store.snapshot(datetime.now(timezone.utc))["stale"] is False
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.close()

    asyncio.run(run())


def test_day_refresh_can_advance_twice_in_one_six_hour_block(tmp_path):
    noon = datetime(2026, 9, 6, 4, tzinfo=timezone.utc)  # Sunday 12:00, no class boundary.
    store = DailyLifeStore(tmp_path / "life.sqlite3")

    class Gateway:
        calls = 0

        async def complete(self, _messages, **_kwargs):
            self.calls += 1
            return SimpleNamespace(text=json.dumps(life_decision(_messages, kind='practice', focus=f'第{self.calls}段')))

    gateway = Gateway()
    runtime = DailyLifeRuntime(store, lambda: gateway, lambda: "林离喜欢弹琴。")

    async def run():
        await runtime.refresh(noon)
        assert gateway.calls == 1
        await runtime.refresh(noon + timedelta(minutes=59))
        assert gateway.calls == 1
        later = noon + timedelta(hours=1)
        assert store.snapshot(later)["stale"] is True
        await runtime.refresh(later)
        assert gateway.calls == 2
        for minute in range(1, 10):
            await runtime.refresh(later + timedelta(minutes=minute))
        assert gateway.calls == 2  # Polling does not pay for a new decision each minute.
        assert store.snapshot(later)["current"]["note"] == "在住处练琴：第2段。"

    asyncio.run(run())


def test_overdue_life_waits_for_waking_hours(tmp_path):
    evening = datetime(2026, 9, 7, 13, tzinfo=timezone.utc)  # 21:00 in Shanghai
    store = DailyLifeStore(tmp_path / "life.sqlite3")

    class Gateway:
        calls = 0

        async def complete(self, _messages, **_kwargs):
            self.calls += 1
            return SimpleNamespace(text=json.dumps(life_decision(_messages, kind='housework', focus=f'第{self.calls}页曲谱')))

    gateway = Gateway()
    runtime = DailyLifeRuntime(store, lambda: gateway, lambda: "林离喜欢弹琴。")

    async def run():
        await runtime.refresh(evening)
        assert gateway.calls == 1
        overnight = evening + timedelta(hours=5, minutes=31)
        assert store.snapshot(overnight)["stale"] is True
        await runtime.refresh(overnight)
        assert gateway.calls == 1
        await runtime.refresh(evening + timedelta(hours=11, minutes=31))
        assert gateway.calls == 2

    asyncio.run(run())


def test_refresh_backoff_survives_restart_and_success_clears_state(tmp_path):
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    gateway = FailingThenSuccessfulGateway()
    runtime = DailyLifeRuntime(store, lambda: gateway, lambda: "林离喜欢弹琴。")

    async def run():
        await runtime.refresh(NOW)
        row = _retry_row(store, _source_id(NOW))
        assert row is not None
        assert row[0] == 1
        assert datetime.fromisoformat(row[1]) == NOW + _REFRESH_RETRY_INITIAL

        restarted = DailyLifeRuntime(store, lambda: gateway, lambda: "林离喜欢弹琴。")
        await restarted.refresh(NOW + timedelta(minutes=1))
        assert gateway.calls == 1

        await restarted.refresh(NOW + _REFRESH_RETRY_INITIAL)
        assert gateway.calls == 2
        assert _retry_row(store, _source_id(NOW)) is None
        assert store.snapshot(NOW + _REFRESH_RETRY_INITIAL)["stale"] is False

    asyncio.run(run())


def test_refresh_failure_count_is_capped_per_time_block_and_recovers_next_block(tmp_path):
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    gateway = AlwaysFailingGateway()
    runtime = DailyLifeRuntime(store, lambda: gateway, lambda: "林离喜欢弹琴。")

    async def run():
        when = NOW
        for failure_count in range(1, _REFRESH_FAILURE_LIMIT + 1):
            await runtime.refresh(when)
            assert gateway.calls == failure_count
            row = _retry_row(store, _source_id(when))
            assert row is not None and row[0] == failure_count
            retry_after = datetime.fromisoformat(row[1])
            if failure_count < _REFRESH_FAILURE_LIMIT:
                expected_delay = min(
                    _REFRESH_RETRY_INITIAL * (2 ** (failure_count - 1)),
                    _REFRESH_RETRY_MAX,
                )
                assert retry_after == when + expected_delay
                when = retry_after + timedelta(seconds=1)
            else:
                assert retry_after == runtime._refresh_block_end(when)

        await runtime.refresh(NOW + timedelta(hours=4))
        assert gateway.calls == _REFRESH_FAILURE_LIMIT

        next_block = runtime._refresh_block_end(NOW) + timedelta(seconds=1)
        await runtime.refresh(next_block)
        assert gateway.calls == _REFRESH_FAILURE_LIMIT + 1
        assert _retry_row(store, _source_id(next_block))[0] == 1

    asyncio.run(run())


def test_refresh_storage_failure_falls_back_to_memory_block_and_does_not_repeat(tmp_path):
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    gateway = AlwaysFailingGateway()
    runtime = DailyLifeRuntime(store, lambda: gateway, lambda: "����ϲ�����١�")

    async def run():
        original_db = store._db

        async def first_failure_then_storage_break(_messages, **_kwargs):
            gateway.calls += 1

            def broken_db():
                raise sqlite3.OperationalError("synthetic retry storage failure")

            store._db = broken_db
            raise RuntimeError("synthetic provider failure")

        gateway.complete = first_failure_then_storage_break
        await runtime.refresh(NOW)
        assert gateway.calls == 1
        assert runtime._memory_retry_source_id == _source_id(NOW)
        assert runtime._memory_retry_after == runtime._refresh_block_end(NOW)

        await runtime.refresh(NOW + timedelta(minutes=1))
        assert gateway.calls == 1
        store._db = original_db

    asyncio.run(run())


@pytest.mark.parametrize(
    ("code", "status"),
    [("PROVIDER_QUOTA_EXHAUSTED", 402), ("INVALID_API_KEY", None)],
)
def test_quota_or_auth_failure_breaks_the_current_block_immediately(tmp_path, code, status):
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    gateway = HardFailingGateway(code, status)
    runtime = DailyLifeRuntime(store, lambda: gateway, lambda: "����ϲ�����١�")

    async def run():
        await runtime.refresh(NOW)
        assert gateway.calls == 1
        row = _retry_row(store, _source_id(NOW))
        assert row is not None and row[0] == _REFRESH_FAILURE_LIMIT
        assert datetime.fromisoformat(row[1]) == runtime._refresh_block_end(NOW)

        await runtime.refresh(NOW + timedelta(minutes=2))
        assert gateway.calls == 1

    asyncio.run(run())

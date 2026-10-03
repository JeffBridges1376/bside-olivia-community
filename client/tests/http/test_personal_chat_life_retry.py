import asyncio
import json
from types import SimpleNamespace

import pytest

from runtime.personal_chat import backend
from runtime.private_world.jev_exchange import EXCHANGE_ERROR_CODES


def server_for(schedule, tasks=None):
    return SimpleNamespace(daily_life_tasks={} if tasks is None else tasks,
                           _schedule_daily_life_exchange=schedule,
                           _persist_store_state=lambda: None)


@pytest.mark.parametrize('reason', sorted(EXCHANGE_ERROR_CODES))
def test_rejected_exchange_result_stops_paid_retries_after_restart(monkeypatch, reason):
    scheduled, persisted = [], []
    row = dict(letter_id='synthetic', daily_life_status='PENDING')

    async def persist(_server):
        persisted.append(json.loads(json.dumps(row)))

    monkeypatch.setattr(backend, 'persist_chat', persist)

    def schedule(current):
        scheduled.append(current['letter_id'])

        async def rejected():
            current.update(daily_life_error_code='DAILY_LIFE_EXCHANGE_UNAVAILABLE',
                           daily_life_failure_reason=reason)

        server.daily_life_tasks['reply:synthetic:1'] = asyncio.create_task(rejected())

    server = server_for(schedule)

    async def run():
        with pytest.raises(RuntimeError, match='PERSONAL_CHAT_DAILY_LIFE_UNAVAILABLE'):
            await backend._commit_life(server, row)
        assert row['daily_life_retry_status'] == 'TERMINAL_REJECTION'
        assert row['daily_life_status'] == 'PENDING'
        assert row['daily_life_failure_reason'] == reason
        assert persisted[-1] == row
        restored = json.loads(json.dumps(row))
        restarted = server_for(lambda current: scheduled.append('unexpected'))
        await backend._commit_life(restarted, restored)
        assert restored == row
        assert scheduled == ['synthetic']
        assert restored['daily_life_attempts'] == 1

    asyncio.run(run())


def test_saved_rejected_result_is_marked_terminal_without_another_extraction():
    scheduled = []
    row = dict(letter_id='synthetic', daily_life_status='PENDING', daily_life_attempts=1,
               daily_life_failure_reason='JEV_EXCHANGE_SLOT_CONFLICT')
    asyncio.run(backend._commit_life(server_for(lambda current: scheduled.append(current)), row))
    assert row['daily_life_retry_status'] == 'TERMINAL_REJECTION'
    assert row['daily_life_status'] == 'PENDING'
    assert scheduled == []


@pytest.mark.parametrize('reason', ['JEV_HTTP_503', 'JEV_UNAVAILABLE', 'JEV_INPUT_TOO_LARGE'])
def test_provider_or_preflight_failure_keeps_bounded_retry_policy(reason):
    scheduled = []
    row = dict(letter_id='synthetic', daily_life_status='PENDING', daily_life_failure_reason=reason)
    server = server_for(lambda current: scheduled.append(current['letter_id']))

    async def run():
        for _ in range(backend._LIFE_ATTEMPTS):
            with pytest.raises(RuntimeError, match='PERSONAL_CHAT_DAILY_LIFE_UNAVAILABLE'):
                await backend._commit_life(server, row)
        await backend._commit_life(server, row)
        assert len(scheduled) == row['daily_life_attempts'] == backend._LIFE_ATTEMPTS
        assert 'daily_life_retry_status' not in row
        assert row['daily_life_failure_reason'] == reason

    asyncio.run(run())


def test_timeout_reuses_running_attempt_even_at_retry_limit():
    scheduled = []

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        row = dict(letter_id='synthetic', daily_life_status='PENDING',
                   daily_life_attempts=backend._LIFE_ATTEMPTS - 1)

        async def extraction():
            entered.set()
            await release.wait()
            row['daily_life_status'] = 'COMMITTED'

        def schedule(current):
            scheduled.append(current['letter_id'])
            server.daily_life_tasks['reply:synthetic:1'] = asyncio.create_task(extraction())

        server = server_for(schedule)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(backend._commit_life(server, row), .02)
        await entered.wait()
        task = server.daily_life_tasks['reply:synthetic:1']
        assert not task.done() and not task.cancelled()
        resumed = asyncio.create_task(backend._commit_life(server, row))
        await asyncio.sleep(0)
        assert not resumed.done()
        assert scheduled == ['synthetic']
        assert row['daily_life_attempts'] == backend._LIFE_ATTEMPTS
        release.set()
        await resumed
        assert row['daily_life_status'] == 'COMMITTED'

    asyncio.run(run())


def test_active_attempt_is_awaited_when_previous_result_was_rejected():
    async def run():
        row = dict(letter_id='synthetic', daily_life_status='PENDING', daily_life_attempts=1,
                   daily_life_failure_reason='JEV_EXCHANGE_SLOT_CONFLICT',
                   daily_life_retry_status='TERMINAL_REJECTION')

        async def recovered():
            row['daily_life_status'] = 'COMMITTED'
            row.pop('daily_life_failure_reason')

        task = asyncio.create_task(recovered())
        server = server_for(lambda current: pytest.fail('must not start a second extraction'),
                            {'reply:synthetic:1': task})
        await backend._commit_life(server, row)
        assert task.done()
        assert row['daily_life_status'] == 'COMMITTED'
        assert 'daily_life_retry_status' not in row

    asyncio.run(run())


def test_terminal_life_replay_still_commits_other_consumers(monkeypatch):
    calls = []

    def consumer(name):
        async def consume(_server, _row):
            calls.append(name)
        return consume

    for name in ('_commit_mailbox_notice', '_commit_world', '_commit_candidates', '_commit_memory'):
        monkeypatch.setattr(backend, name, consumer(name))
    import runtime.image_understanding
    monkeypatch.setattr(runtime.image_understanding, 'commit_image_memory', consumer('image_memory'))
    row = dict(letter_id='synthetic', delivery_status='DELIVERED', daily_life_status='PENDING',
               daily_life_failure_reason='JEV_EXCHANGE_SLOT_CONFLICT',
               daily_life_retry_status='TERMINAL_REJECTION')
    server = server_for(lambda current: pytest.fail('terminal extraction must not be rescheduled'))
    asyncio.run(backend.commit(server, row))
    assert calls == ['_commit_mailbox_notice', '_commit_world', '_commit_candidates',
                     '_commit_memory', 'image_memory']
    assert row['daily_life_status'] == 'PENDING'

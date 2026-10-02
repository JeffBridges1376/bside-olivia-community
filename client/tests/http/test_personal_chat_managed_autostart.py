from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest


class _Store:
    def __init__(self) -> None:
        self.letters = []


class _Server:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = _Store()
        self.private_world_port = None

    def _state_root(self) -> Path:
        return self.root


def test_managed_qq_component_starts_with_olivia(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.personal_chat import napcat_installer, setup

    config = tmp_path / "personal-chat" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "qq": {
                    "url": "ws://127.0.0.1:3001",
                    "account": "123456789",
                    "owner": "987654321",
                    "credentials_file": str(tmp_path / "personal-chat" / "qq.dpapi"),
                    "managed": True,
                }
            }
        ),
        encoding="utf-8",
    )
    server = _Server(tmp_path)
    process = object()
    calls: list[Path] = []

    def fake_ensure(root: Path):
        calls.append(root)
        return process

    monkeypatch.setattr(napcat_installer, "ensure_shell", fake_ensure)
    monkeypatch.setattr(napcat_installer, "onebot_available", lambda: True)
    monkeypatch.setattr(napcat_installer, "managed_connection", lambda _root: ("ws://127.0.0.1:3001", "synthetic-token-123456789"))
    monkeypatch.setattr(napcat_installer, "account_config_ready", lambda _root, _account: True)
    async def fake_probe(_url, _token, _account=None):
        return "123456789"
    monkeypatch.setattr(setup, "_qq_probe", fake_probe)
    app = web.Application()
    setup.install_setup_routes(app, server)
    runtime = app[setup._SETUP]

    async def scenario() -> None:
        async with TestClient(TestServer(app)):
            # QQ starts in the background; the server is already serving.
            await runtime["napcat_start_task"]
            assert calls == [tmp_path]
            assert runtime["napcat_shell_process"] is process
            assert runtime["napcat_state"] == "ONEBOT_READY"

    asyncio.run(scenario())


def test_managed_qq_watchdog_restarts_dead_napcat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.personal_chat import napcat_installer, setup

    config = tmp_path / "personal-chat" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "qq": {
                    "url": "ws://127.0.0.1:3001",
                    "account": "123456789",
                    "owner": "987654321",
                    "credentials_file": str(tmp_path / "personal-chat" / "qq.dpapi"),
                    "managed": True,
                }
            }
        ),
        encoding="utf-8",
    )

    class Process:
        def __init__(self, alive: bool) -> None:
            self.alive = alive

        def poll(self):
            return None if self.alive else 1

    first = Process(True)
    second = Process(True)
    processes = [first, second]
    calls: list[Path] = []
    availability = {"value": True}

    def fake_ensure(root: Path):
        calls.append(root)
        return processes[min(len(calls) - 1, 1)]

    server = _Server(tmp_path)
    monkeypatch.setattr(setup, "_NAPCAT_WATCHDOG_SECONDS", 0.01)
    monkeypatch.setattr(napcat_installer, "ensure_shell", fake_ensure)
    monkeypatch.setattr(
        napcat_installer, "onebot_available", lambda: availability["value"]
    )
    monkeypatch.setattr(napcat_installer, "webui_available", lambda _root: False)
    monkeypatch.setattr(napcat_installer, "managed_connection", lambda _root: ("ws://127.0.0.1:3001", "synthetic-token-123456789"))
    monkeypatch.setattr(napcat_installer, "account_config_ready", lambda _root, _account: True)
    async def fake_probe(_url, _token, _account=None):
        return "123456789"
    monkeypatch.setattr(setup, "_qq_probe", fake_probe)

    app = web.Application()
    setup.install_setup_routes(app, server)
    runtime = app[setup._SETUP]

    async def scenario() -> None:
        async with TestClient(TestServer(app)):
            await runtime["napcat_start_task"]
            assert runtime["napcat_shell_process"] is first
            first.alive = False
            availability["value"] = False
            for _ in range(100):
                await asyncio.sleep(0.01)
                if runtime["napcat_shell_process"] is second:
                    break
            assert runtime["napcat_shell_process"] is second
            assert len(calls) >= 2

    asyncio.run(scenario())


def _managed_config(tmp_path: Path) -> None:
    config = tmp_path / "personal-chat" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"qq": {
        "url": "ws://127.0.0.1:3001", "account": "123456789", "owner": "987654321",
        "credentials_file": str(tmp_path / "personal-chat" / "qq.dpapi"), "managed": True}}), encoding="utf-8")


@pytest.mark.parametrize("answers,restarts", [
    ([False, False], 1),          # kicked offline: restart so NapCat quick-logs in again
    ([False, True, False], 0),    # a single offline moment recovers on its own
    ([True, True], 0),
])
def test_managed_qq_watchdog_relogs_in_after_qq_kicks_the_account_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answers, restarts
) -> None:
    from runtime.personal_chat import napcat_installer, setup

    _managed_config(tmp_path)

    class Process:
        def poll(self):
            return None

    first, second = Process(), Process()
    started: list = []
    stopped: list = []
    remaining = list(answers)

    def fake_ensure(_root: Path):
        started.append(_root)
        return first if len(started) == 1 else second

    async def fake_online(_url, _token):
        if not remaining:
            await asyncio.sleep(3600)
        return remaining.pop(0)

    async def fake_probe(_url, _token, _account=None):
        return "123456789"

    monkeypatch.setattr(setup, "_NAPCAT_WATCHDOG_SECONDS", 0.01)
    monkeypatch.setattr(setup, "_NAPCAT_ONLINE_CHECK_SECONDS", 0.0)
    monkeypatch.setattr(napcat_installer, "ensure_shell", fake_ensure)
    monkeypatch.setattr(napcat_installer, "stop_shell", lambda _root, process: stopped.append(process))
    monkeypatch.setattr(napcat_installer, "onebot_available", lambda: True)
    monkeypatch.setattr(napcat_installer, "managed_connection", lambda _root: ("ws://127.0.0.1:3001", "synthetic-token-123456789"))
    monkeypatch.setattr(napcat_installer, "account_config_ready", lambda _root, _account: True)
    monkeypatch.setattr(napcat_installer, "remember_account", lambda _root, _account: True)
    monkeypatch.setattr(setup, "_qq_probe", fake_probe)
    monkeypatch.setattr(setup, "_qq_online", fake_online)

    app = web.Application()
    setup.install_setup_routes(app, _Server(tmp_path))
    runtime = app[setup._SETUP]

    async def scenario() -> None:
        async with TestClient(TestServer(app)):
            await runtime["napcat_start_task"]
            for _ in range(200):
                await asyncio.sleep(0.01)
                if not remaining:
                    break
            await asyncio.sleep(0.05)
            assert stopped == [first] * restarts
            assert len(started) == 1 + restarts
            assert runtime["napcat_shell_process"] is (second if restarts else first)
            assert runtime["napcat_state"] == "ONEBOT_READY"

    asyncio.run(scenario())

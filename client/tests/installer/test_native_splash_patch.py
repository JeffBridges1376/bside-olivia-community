import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from installer import patch_native_splash as patch


def fixture(monkeypatch):
    data = bytearray(b'original-engine-and-ui' + b'\0' * 100)
    data[25:31] = patch.DISPLAY_CALL
    monkeypatch.setattr(patch, 'DISPLAY_OFFSET', 25)
    monkeypatch.setattr(patch, 'ORIGINAL_SHA256', hashlib.sha256(data).hexdigest())
    return bytes(data)


def test_only_display_call_changes_and_backup_is_idempotent(tmp_path, monkeypatch):
    data = fixture(monkeypatch)
    target = tmp_path / 'NutApp.dll'
    target.write_bytes(data)
    assert patch.patch_native_splash(tmp_path) == 'patched'
    revised = target.read_bytes()
    assert revised[:25] == data[:25] and revised[31:] == data[31:]
    assert revised[25:31] == b'\x90' * 6
    assert (tmp_path / 'NutApp.dll.native-splash.orig').read_bytes() == data
    assert patch.patch_native_splash(tmp_path) == 'already_patched'


def test_unknown_dll_and_backup_fail_without_mutating_target(tmp_path, monkeypatch):
    data = fixture(monkeypatch)
    target = tmp_path / 'NutApp.dll'
    target.write_bytes(data + b'unknown')
    with pytest.raises(patch.NativeSplashPatchError):
        patch.patch_native_splash(tmp_path)
    assert target.read_bytes() == data + b'unknown'
    target.write_bytes(data)
    (tmp_path / 'NutApp.dll.native-splash.orig').write_bytes(b'unrelated')
    with pytest.raises(patch.NativeSplashPatchError):
        patch.patch_native_splash(tmp_path)
    assert target.read_bytes() == data


@pytest.mark.parametrize('supported', [True, False])
def test_launcher_patches_before_video_and_unknown_dll_still_launches(tmp_path, monkeypatch, supported):
    from installer import start_local
    from installer.native_window_layout import LayoutStatus
    video = tmp_path / 'startup.mp4'
    video.write_bytes(b'synthetic-video')
    calls = []
    def prepare(root):
        calls.append('patch')
        if not supported:
            raise patch.NativeSplashPatchError('unsupported')
        return 'patched'
    monkeypatch.setattr(patch, 'patch_native_splash', prepare)
    def spawn(command, **kwargs):
        kind = 'video' if command[0] == 'powershell.exe' else 'client'
        calls.append(kind)
        if kind == 'video':
            assert command[command.index('-ClientProcessId') + 1] == '1234'
        return SimpleNamespace(pid=1234, wait=lambda: calls.append('wait') or 0)
    monkeypatch.setattr(start_local.subprocess, 'Popen', spawn)
    monkeypatch.setattr(start_local.subprocess, 'call', lambda *args, **kwargs: calls.append('client') or 0)
    monkeypatch.setattr(start_local, 'guard_native_window_layout', lambda *args, **kwargs: LayoutStatus.SKIPPED)
    monkeypatch.setattr(start_local, '_append_launcher_event', lambda *args, **kwargs: None)
    result = start_local._run_client_with_native_layout(tmp_path / 'Olivia.exe', tmp_path,
        cwd=tmp_path, environment={'OLIVIA_STARTUP_VIDEO': str(video)}, data_root=tmp_path, attempt=1)
    assert result == 0
    assert calls == ['patch', 'client', 'video', 'wait']


def test_player_spawn_failure_does_not_cancel_started_client(tmp_path, monkeypatch):
    from installer import start_local
    from installer.native_window_layout import LayoutStatus
    video = tmp_path / 'startup.mp4'
    video.write_bytes(b'fixture')
    calls = []
    def spawn(command, **kwargs):
        if command[0] == 'powershell.exe':
            raise OSError('player missing')
        calls.append('client')
        return SimpleNamespace(pid=1234, wait=lambda: calls.append('wait') or 7)
    monkeypatch.setattr(patch, 'patch_native_splash', lambda _: 'already_patched')
    monkeypatch.setattr(start_local.subprocess, 'Popen', spawn)
    monkeypatch.setattr(start_local, 'guard_native_window_layout', lambda *a, **k: LayoutStatus.SKIPPED)
    monkeypatch.setattr(start_local, '_append_launcher_event', lambda *a, **k: None)
    assert start_local._run_client_with_native_layout(tmp_path/'Olivia.exe', tmp_path,
        cwd=tmp_path, environment={'OLIVIA_STARTUP_VIDEO': str(video)}, data_root=tmp_path, attempt=1) == 7
    assert calls == ['client', 'wait']

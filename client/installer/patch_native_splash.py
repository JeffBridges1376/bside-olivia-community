"""Suppress only the known 0.0.9.627 native SplashScreen display call.

Its construction, resource loading, destruction and QApplication event loop remain
unchanged. The launcher owns the replacement startup video. Unknown DLLs are never
patched. This does not touch a running process or other native windows.
"""
import hashlib
import os
from pathlib import Path
import uuid

ORIGINAL_SHA256 = '23bd2c414a2ca59fd193ed618a151c5fc479e1e7d7ed83dc43f858e1157fa798'
DISPLAY_OFFSET = 0xFEB5
DISPLAY_CALL = bytes.fromhex('ff1555500400')
SUPPRESSED_CALL = b'\x90' * 6


class NativeSplashPatchError(ValueError):
    pass


def patched_bytes(data):
    candidate = bytearray(data)
    candidate[DISPLAY_OFFSET:DISPLAY_OFFSET + 6] = DISPLAY_CALL
    if hashlib.sha256(candidate).hexdigest() != ORIGINAL_SHA256:
        raise NativeSplashPatchError('NATIVE_SPLASH_UNSUPPORTED_DLL')
    if data[DISPLAY_OFFSET:DISPLAY_OFFSET + 6] not in (DISPLAY_CALL, SUPPRESSED_CALL):
        raise NativeSplashPatchError('NATIVE_SPLASH_UNSUPPORTED_CALL')
    candidate[DISPLAY_OFFSET:DISPLAY_OFFSET + 6] = SUPPRESSED_CALL
    return bytes(candidate)


def patch_native_splash(client_root):
    path = Path(client_root) / 'NutApp.dll'
    original = path.read_bytes()
    updated = patched_bytes(original)
    if original == updated:
        return 'already_patched'
    backup = path.with_name('NutApp.dll.native-splash.orig')
    if backup.exists():
        if hashlib.sha256(backup.read_bytes()).hexdigest() != ORIGINAL_SHA256:
            raise NativeSplashPatchError('NATIVE_SPLASH_BACKUP_MISMATCH')
    else:
        with backup.open('xb') as output:
            output.write(original)
    temporary = path.with_name(f'.NutApp.{uuid.uuid4().hex}.tmp')
    try:
        temporary.write_bytes(updated)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return 'patched'

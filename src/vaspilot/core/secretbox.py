"""Windows DPAPI (CurrentUser) secret protection for local credentials.

The web console can save an API key or an explicitly opted-in server
credential. It is encrypted with CryptProtectData (user scope + an
application entropy string) and persisted as base64 inside
``~/.vaspilot/settings.json``. Plaintext exists only in memory during
decryption for one outgoing request. Same-user processes can decrypt — the
normal Windows DPAPI CurrentUser boundary.

Non-Windows platforms: this module refuses; users configure ``api_key_env``
instead.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes

APP_ENTROPY = b"VASPilot-provider-key/v1"
SERVER_CREDENTIAL_ENTROPY = b"VASPilot-server-credential/v1"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> _DATA_BLOB:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buffer,
                                             ctypes.POINTER(ctypes.c_byte)))


def available() -> bool:
    return hasattr(ctypes, "windll") and hasattr(
        ctypes.windll, "crypt32")


def _local_free(pointer) -> None:
    ctypes.windll.kernel32.LocalFree(pointer)


def protect(data: bytes, entropy: bytes = APP_ENTROPY) -> bytes:
    """Encrypt ``data`` with DPAPI; returns the raw ciphertext bytes."""
    if not available():
        raise OSError("DPAPI is only available on Windows")
    din = _blob(data)
    dent = _blob(entropy)
    dout = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(din), None, ctypes.byref(dent), None, None, 0,
        ctypes.byref(dout))
    if not ok:
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(dout.pbData, dout.cbData)
    finally:
        _local_free(dout.pbData)


def unprotect(data: bytes, entropy: bytes = APP_ENTROPY) -> bytes:
    """Decrypt DPAPI ciphertext; raises OSError when wrong user/tampered."""
    if not available():
        raise OSError("DPAPI is only available on Windows")
    din = _blob(data)
    dent = _blob(entropy)
    dout = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(din), None, ctypes.byref(dent), None, None, 0,
        ctypes.byref(dout))
    if not ok:
        raise OSError("CryptUnprotectData failed (wrong user or tampered)")
    try:
        return ctypes.string_at(dout.pbData, dout.cbData)
    finally:
        _local_free(dout.pbData)


def protect_b64(text: str, *, entropy: bytes = APP_ENTROPY) -> str:
    return base64.b64encode(
        protect(text.encode("utf-8"), entropy=entropy)).decode("ascii")


def unprotect_b64(encoded: str, *, entropy: bytes = APP_ENTROPY) -> str:
    raw = unprotect(base64.b64decode(encoded), entropy=entropy)
    return raw.decode("utf-8")


def server_credential_entropy(server: str) -> bytes:
    """Bind a server credential blob to its local server name.

    This does not replace DPAPI's user binding; it also prevents an encrypted
    entry being copied to a different server name and silently reused there.
    """
    return SERVER_CREDENTIAL_ENTROPY + b":" + str(server).encode("utf-8")

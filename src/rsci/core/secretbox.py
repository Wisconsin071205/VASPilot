"""Local secret protection for credentials stored under ``~/.rsci``.

Windows uses DPAPI (CurrentUser) so the ciphertext is bound to the logged-in
user.  POSIX systems use an AES-256-GCM key stored in a user-only (0600) key
file; the key is derived per-purpose with HKDF, so a blob copied to another
entropy label cannot be decrypted accidentally.  Both backends expose the
same ``protect``/``unprotect``/``protect_b64``/``unprotect_b64`` API.

Plaintext exists only in memory while one credential is used.  If no backend
is available (for example POSIX without ``cryptography``), callers must fall
back to an explicit environment variable or refuse to persist the secret.
"""

from __future__ import annotations

import base64
import ctypes
import os
import secrets
import stat
import tempfile
from pathlib import Path

try:  # Present on Windows; importing it is harmless elsewhere.
    from ctypes import wintypes as _wintypes
except Exception:  # pragma: no cover - only relevant on unusual builds
    _wintypes = None

APP_ENTROPY = b"RSCI-provider-key/v1"
SERVER_CREDENTIAL_ENTROPY = b"RSCI-server-credential/v1"
WEBDAV_ENTROPY = b"RSCI-webdav-credential/v1"

_POSIX_MAGIC = b"RSCI1"
_NONCE_BYTES = 12
_KEY_BYTES = 32


# ---------------------------------------------------------------------------
# Windows DPAPI
# ---------------------------------------------------------------------------
if hasattr(ctypes, "windll"):

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", _wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))]

    def _blob(data: bytes) -> _DATA_BLOB:
        buffer = ctypes.create_string_buffer(data, len(data))
        return _DATA_BLOB(len(data), ctypes.cast(
            buffer, ctypes.POINTER(ctypes.c_byte)))

    def _local_free(pointer) -> None:
        ctypes.windll.kernel32.LocalFree(pointer)

    def _dpapi_available() -> bool:
        return hasattr(ctypes.windll, "crypt32")

    def _dpapi_protect(data: bytes, entropy: bytes) -> bytes:
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

    def _dpapi_unprotect(data: bytes, entropy: bytes) -> bytes:
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

else:  # pragma: no cover - exercised on POSIX
    def _dpapi_available() -> bool:
        return False

    def _dpapi_protect(data: bytes, entropy: bytes) -> bytes:
        raise OSError("DPAPI is only available on Windows")

    def _dpapi_unprotect(data: bytes, entropy: bytes) -> bytes:
        raise OSError("DPAPI is only available on Windows")


# ---------------------------------------------------------------------------
# Portable AES-GCM backend
# ---------------------------------------------------------------------------
def _posix_crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives import hashes
        return AESGCM, HKDF, hashes
    except Exception:  # ImportError or a broken cryptography install
        return None


def _posix_available() -> bool:
    return os.name != "nt" and _posix_crypto() is not None


def available() -> bool:
    """Whether secrets can be encrypted on this machine."""
    return _dpapi_available() or _posix_available()


def key_path(override: str | Path | None = None) -> Path:
    """Location of the POSIX master key (ignored on Windows)."""
    if override:
        return Path(override).expanduser()
    env_override = os.environ.get("RSCI_SECRET_KEY_FILE", "").strip()
    if env_override:
        return Path(env_override).expanduser()
    home = os.environ.get("RSCI_HOME", "").strip()
    root = Path(home).expanduser() if home else Path.home() / ".rsci"
    return root / "secret.key"


def _read_or_create_key(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = b""
    except OSError as exc:
        raise OSError(f"cannot read local secret key: {exc}") from exc
    if len(data) == _KEY_BYTES:
        return data
    if data:
        raise OSError("local secret key has an unexpected length; "
                      "remove it deliberately to start a new vault")
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = secrets.token_bytes(_KEY_BYTES)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        os.write(fd, fresh)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return fresh


def _derive_key(master: bytes, entropy: bytes) -> bytes:
    crypto = _posix_crypto()
    if crypto is None:
        raise OSError("the 'cryptography' package is required for local "
                      "credential encryption on this platform")
    _, HKDF, hashes = crypto
    return HKDF(algorithm=hashes.SHA256(), length=_KEY_BYTES,
                salt=b"RSCI-posix-secretbox/v1", info=entropy).derive(master)


def _aes_protect(data: bytes, entropy: bytes,
                 key_file: str | Path | None = None) -> bytes:
    crypto = _posix_crypto()
    if crypto is None:
        raise OSError("the 'cryptography' package is required for local "
                      "credential encryption on this platform")
    AESGCM, _, _ = crypto
    master = _read_or_create_key(key_path(key_file))
    key = _derive_key(master, entropy)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, data, entropy)
    return _POSIX_MAGIC + nonce + ciphertext


def _aes_unprotect(data: bytes, entropy: bytes,
                   key_file: str | Path | None = None) -> bytes:
    if not data.startswith(_POSIX_MAGIC) or len(data) <= len(_POSIX_MAGIC) + _NONCE_BYTES:
        raise OSError("local secret blob is malformed")
    crypto = _posix_crypto()
    if crypto is None:
        raise OSError("the 'cryptography' package is required for local "
                      "credential decryption on this platform")
    AESGCM, _, _ = crypto
    master = _read_or_create_key(key_path(key_file))
    key = _derive_key(master, entropy)
    nonce = data[len(_POSIX_MAGIC):len(_POSIX_MAGIC) + _NONCE_BYTES]
    ciphertext = data[len(_POSIX_MAGIC) + _NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, entropy)
    except Exception as exc:  # InvalidTag, ValueError, ...
        raise OSError("local secret decryption failed (wrong key, user, or "
                      "tampered blob)") from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def protect(data: bytes, entropy: bytes = APP_ENTROPY, *,
            key_file: str | Path | None = None) -> bytes:
    """Encrypt ``data`` with the platform backend; returns raw bytes."""
    if _dpapi_available():
        return _dpapi_protect(data, entropy)
    if _posix_available():
        return _aes_protect(data, entropy, key_file)
    raise OSError("no local credential encryption backend is available")


def unprotect(data: bytes, entropy: bytes = APP_ENTROPY, *,
              key_file: str | Path | None = None) -> bytes:
    """Decrypt a blob produced by :func:`protect` on this machine."""
    if data.startswith(_POSIX_MAGIC):
        return _aes_unprotect(data, entropy, key_file)
    if _dpapi_available():
        return _dpapi_unprotect(data, entropy)
    raise OSError("no local credential encryption backend is available")


def protect_b64(text: str, *, entropy: bytes = APP_ENTROPY,
                key_file: str | Path | None = None) -> str:
    return base64.b64encode(
        protect(text.encode("utf-8"), entropy=entropy,
                key_file=key_file)).decode("ascii")


def unprotect_b64(encoded: str, *, entropy: bytes = APP_ENTROPY,
                  key_file: str | Path | None = None) -> str:
    raw = unprotect(base64.b64decode(encoded), entropy=entropy,
                    key_file=key_file)
    return raw.decode("utf-8")


def server_credential_entropy(server: str) -> bytes:
    """Bind a server credential blob to its local server name.

    This does not replace the platform's user binding; it also prevents an
    encrypted entry being copied to a different server name and silently
    reused there.
    """
    return SERVER_CREDENTIAL_ENTROPY + b":" + str(server).encode("utf-8")


def webdav_credential_entropy(username: str) -> bytes:
    """Bind a WebDAV password blob to its username and purpose."""
    return WEBDAV_ENTROPY + b":" + str(username or "").encode("utf-8")

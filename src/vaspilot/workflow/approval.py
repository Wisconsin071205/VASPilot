"""One-shot approval tokens bound to a specific plan execution context.

An approval binds ``server + plan_hash + files_hash + action + parameter_hash
+ expiry`` and is signed with the per-install HMAC key from
``<config>/approval.key``. Tokens are one-shot: the ledger records the run
(or submit) that consumed a token, and any replay for a different purpose
fails.

The signature chain is entirely local: a model process can neither mint nor
verify tokens because it never sees the key. ``workflow approve`` is the only
minting path and it requires an interactive confirmation in a trusted local
terminal.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..core.errors import ApprovalError
from ..core.hashing import canonical_json

_SERVER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_TOKEN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_PAYLOAD_KEYS = {"token_id", "server", "plan_hash", "files_hash", "action",
                 "parameter_hash", "expires_at"}

ACTIONS = ("workflow_run", "job_submit")
DEFAULT_VALIDITY_HOURS = 24


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ApprovalError("approval expiry is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ApprovalError("approval expiry must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass
class ApprovalToken:
    server: str
    plan_hash: str
    files_hash: str
    action: str
    parameter_hash: str
    expires_at: str
    signature: str = ""
    token_id: str = field(default_factory=lambda: secrets.token_hex(8))

    def payload(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "server": self.server,
            "plan_hash": self.plan_hash,
            "files_hash": self.files_hash,
            "action": self.action,
            "parameter_hash": self.parameter_hash,
            "expires_at": self.expires_at,
        }

    def encode(self) -> str:
        payload = canonical_json(self.payload()).rstrip("\n").encode("utf-8")
        b64 = base64.urlsafe_b64encode(payload).decode("ascii")
        return f"{b64}.{self.signature}"


def _sign(payload_json: str, key: bytes) -> str:
    return hmac.new(key, payload_json.encode("utf-8"), hashlib.sha256).hexdigest()


def _validate(token: ApprovalToken) -> None:
    if not _TOKEN_ID_RE.fullmatch(token.token_id):
        raise ApprovalError("approval token id is invalid")
    if not _SERVER_RE.fullmatch(token.server):
        raise ApprovalError("approval server is invalid")
    if not _HASH_RE.fullmatch(token.plan_hash):
        raise ApprovalError("approval plan hash must be lowercase SHA-256")
    if not _HASH_RE.fullmatch(token.files_hash):
        raise ApprovalError("approval files hash must be lowercase SHA-256")
    if not _ACTION_RE.fullmatch(token.action) or token.action not in ACTIONS:
        raise ApprovalError("approval action is invalid")
    if not _HASH_RE.fullmatch(token.parameter_hash):
        raise ApprovalError("approval parameter hash must be lowercase SHA-256")
    _parse_iso(token.expires_at)


def issue_token(key: bytes, *, server: str, plan_hash: str, files_hash: str,
                action: str, parameter_hash: str,
                validity_hours: int = DEFAULT_VALIDITY_HOURS,
                now: datetime | None = None) -> ApprovalToken:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ApprovalError("approval signing key must hold 32+ bytes")
    start = now or _utcnow()
    expires = start + timedelta(hours=max(1, int(validity_hours)))
    token = ApprovalToken(
        server=server, plan_hash=plan_hash, files_hash=files_hash,
        action=action, parameter_hash=parameter_hash,
        expires_at=expires.isoformat(timespec="seconds"))
    _validate(token)
    payload_json = canonical_json(token.payload()).rstrip("\n")
    token.signature = _sign(payload_json, key)
    return token


def decode_token(encoded: str) -> ApprovalToken:
    try:
        b64, sig = str(encoded).rsplit(".", 1)
        padded = b64 + "=" * (-len(b64) % 4)
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_",
                               validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error,
            json.JSONDecodeError) as exc:
        raise ApprovalError("malformed approval token") from exc
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
        raise ApprovalError("malformed approval token payload")
    token = ApprovalToken(
        token_id=str(payload.get("token_id", "")),
        server=str(payload.get("server", "")),
        plan_hash=str(payload.get("plan_hash", "")),
        files_hash=str(payload.get("files_hash", "")),
        action=str(payload.get("action", "")),
        parameter_hash=str(payload.get("parameter_hash", "")),
        expires_at=str(payload.get("expires_at", "")),
        signature=sig)
    _validate(token)
    if not re.fullmatch(r"[0-9a-f]{64}", token.signature or ""):
        raise ApprovalError("approval signature encoding is invalid")
    return token


def verify_token(key: bytes, encoded: str, *, server: str, plan_hash: str,
                 files_hash: str, action: str, parameter_hash: str,
                 now: datetime | None = None) -> ApprovalToken:
    """Verify signature, bindings and expiry (pure check).

    One-shot semantics live in the engine's ledger: a token may start a run
    exactly once, and only the run instance recorded in the ledger may
    resume with it. Callers enforce that on top of this verification.
    """
    if not isinstance(key, bytes) or len(key) < 32:
        raise ApprovalError("approval signing key must hold 32+ bytes")
    if not encoded:
        raise ApprovalError("approval token is required")
    token = decode_token(encoded)
    payload_json = canonical_json(token.payload()).rstrip("\n")
    expected = _sign(payload_json, key)
    if not hmac.compare_digest(token.signature, expected):
        raise ApprovalError("approval signature is invalid")
    checks = (
        ("server", token.server, server),
        ("plan_hash", token.plan_hash, plan_hash),
        ("files_hash", token.files_hash, files_hash),
        ("action", token.action, action),
        ("parameter_hash", token.parameter_hash, parameter_hash),
    )
    for label, got, want in checks:
        if got != want:
            raise ApprovalError(f"approval binding mismatch on {label}")
    current = now or _utcnow()
    if _parse_iso(token.expires_at) <= current:
        raise ApprovalError("approval token has expired")
    return token

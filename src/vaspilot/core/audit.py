"""Append-only audit log with secret sanitization.

Every remote operation, approval decision and provider-tool call is recorded
as one JSON line under ``<config>/audit/YYYY-MM-DD.jsonl``. Rows are append
only; the writer never rewrites or truncates history.

Sanitization (defense in depth — secrets should never reach this module, but
a leak must not persist):

  - values of keys that look secret-bearing are replaced with ``[REDACTED]``
  - long byte-ish payloads are capped
  - POTCAR content never enters rows: callers pass metadata only
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .hashing import obj_sha256

SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|totp|otp|api_?key|private_?key|"
    r"authorization|bearer|credential)", re.IGNORECASE)
MAX_VALUE_CHARS = 400
REDACTED = "[REDACTED]"

_write_lock = threading.Lock()


def sanitize(value, *, depth: int = 0):
    """Recursively redact secret-looking values and cap enormous payloads."""
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_text = key if isinstance(key, str) else str(key)
            if SECRET_KEY_RE.search(key_text):
                out[key_text] = REDACTED
            else:
                out[key_text] = sanitize(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize(item, depth=depth + 1) for item in value[:64]]
    if isinstance(value, str):
        return value if len(value) <= MAX_VALUE_CHARS else value[:MAX_VALUE_CHARS] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


class AuditLog:
    """Append-only JSONL audit log, one file per UTC day."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def record(self, event: str, *, outcome: str = "ok", **fields) -> dict:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": str(event)[:64],
            "outcome": str(outcome)[:32],
            **fields,
        }
        row = sanitize(row)
        row["row_sha256"] = obj_sha256(row)
        self._append(row)
        return row

    def _append(self, row: dict) -> None:
        day = row["ts"][:10]
        path = self.directory / f"audit-{day}.jsonl"
        line = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        with _write_lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            # O_APPEND keeps concurrent writers from clobbering each other;
            # O_BINARY stops MSVCRT text-mode newline translation.
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
            fd = os.open(path, flags, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)

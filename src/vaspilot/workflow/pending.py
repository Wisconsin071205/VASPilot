"""Pending job submissions: the 'confirm' tier of agent_submit_mode.

When the agent calls job_submit in confirm mode nothing is sent to the
scheduler; a frozen entry (server / directory / script name / script content
digest) is persisted here and shown to the human in the chat UI. Approving
executes exactly the frozen parameters — anything that changed on the remote
side since creation (script digest mismatch) aborts the submit.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError

ID_RE = re.compile(r"^ps-[A-Za-z0-9_-]{4,64}$")
DEFAULT_TTL_SECONDS = 30 * 60
MAX_ENTRIES = 50


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class PendingSubmitStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, entries: dict[str, dict[str, Any]]) -> None:
        if len(entries) > MAX_ENTRIES:  # drop the oldest finalised entries
            ordered = sorted(entries.values(),
                             key=lambda e: str(e.get("created_at", "")))
            for entry in ordered[:len(entries) - MAX_ENTRIES]:
                entries.pop(str(entry.get("id")), None)
        _atomic_write(self.path, entries)

    def _expire(self, entries: dict[str, dict[str, Any]]) -> None:
        now = _utcnow()
        changed = False
        for entry in entries.values():
            if entry.get("status") != "pending":
                continue
            try:
                expires = datetime.fromisoformat(str(entry.get("expires_at")))
            except ValueError:
                continue
            if expires <= now:
                entry["status"] = "expired"
                changed = True
        if changed:
            self._save(entries)

    def create(self, *, server: str, directory: str, script: str,
               script_sha256: str = "", script_content: str = "",
               session_id: str = "",
               ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict[str, Any]:
        import secrets
        entry = {
            "id": f"ps-{secrets.token_urlsafe(9)}",
            "server": str(server),
            "directory": str(directory),
            "script": str(script),
            "script_sha256": str(script_sha256 or ""),
            "script_content": str(script_content or ""),
            "session_id": str(session_id or ""),
            "created_at": _utcnow().isoformat(timespec="seconds"),
            "expires_at": (_utcnow() + timedelta(seconds=ttl_seconds)
                           ).isoformat(timespec="seconds"),
            "status": "pending",
        }
        entries = self._load()
        self._expire(entries)
        entries[entry["id"]] = entry
        self._save(entries)
        return entry

    def get(self, entry_id: str) -> dict[str, Any]:
        if not ID_RE.fullmatch(entry_id or ""):
            raise ValidationError("invalid pending-submit id")
        entries = self._load()
        self._expire(entries)
        entry = entries.get(entry_id)
        if entry is None:
            raise ValidationError(f"pending submit {entry_id!r} not found")
        return entry

    def settle(self, entry_id: str, status: str,
               result: dict[str, Any] | None = None) -> dict[str, Any]:
        if status not in ("approved", "rejected", "expired"):
            raise ValidationError("invalid settle status")
        entries = self._load()
        self._expire(entries)
        entry = entries.get(entry_id)
        if entry is None:
            raise ValidationError(f"pending submit {entry_id!r} not found")
        if entry.get("status") != "pending":
            raise ValidationError(
                f"pending submit {entry_id} is already {entry.get('status')}")
        entry["status"] = status
        entry["settled_at"] = _utcnow().isoformat(timespec="seconds")
        if result is not None:
            entry["result"] = result
        self._save(entries)
        return entry

    def pending(self, session_id: str = "") -> list[dict[str, Any]]:
        entries = self._load()
        self._expire(entries)
        out = [e for e in entries.values() if e.get("status") == "pending"]
        if session_id:
            out = [e for e in out if e.get("session_id") == session_id]
        out.sort(key=lambda e: str(e.get("created_at", "")), reverse=True)
        return out

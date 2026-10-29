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
from functools import wraps
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError
from ..core.locking import exclusive_file_lock

ID_RE = re.compile(r"^ps-[A-Za-z0-9_-]{4,64}$")
DEFAULT_TTL_SECONDS = 30 * 60
MAX_ENTRIES = 50
OPEN_STATUSES = {"pending", "submitting", "uncertain"}


def _locked(method):
    """Serialize each JSON transaction across both threads and processes."""
    @wraps(method)
    def transaction(self, *args, **kwargs):
        with exclusive_file_lock(self.path.with_suffix(".lock")):
            return method(self, *args, **kwargs)
    return transaction


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

    def _load(self, *, strict: bool = False) -> dict[str, dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            if strict:
                raise ValidationError("submission records are unreadable; their state is unknown") from exc
            return {}
        if strict and (not isinstance(data, dict) or
                       any(not isinstance(entry, dict) for entry in data.values())):
            raise ValidationError("submission records are invalid; their state is unknown")
        return data if isinstance(data, dict) else {}

    def inspect(self, *, entry_id: str = "", session_id: str = "",
                server: str = "", directory: str = "", script: str = "",
                limit: int = 10) -> list[dict[str, Any]]:
        """Read bounded evidence, including final states, without changing it.

        Atomic store writes make a read consistent without creating a lock or
        expiring cards on disk. Script bodies and arbitrary result payloads are
        deliberately omitted from the model-facing evidence.
        """
        if entry_id and not ID_RE.fullmatch(entry_id):
            raise ValidationError("invalid pending-submit id")
        entries = self._load(strict=True)
        rows = [entry for entry in entries.values()
                if (not entry_id or entry.get("id") == entry_id)
                and (not session_id or entry.get("session_id") == session_id)
                and (not server or entry.get("server") == server)
                and (not directory or str(entry.get("directory", "")).rstrip("/")
                     == directory.rstrip("/"))
                and (not script or entry.get("script") == script)]
        if entry_id and not rows:
            raise ValidationError("submission record not found in the requested scope")

        def updated_at(entry: dict) -> float:
            stamp = (entry.get("settled_at") or entry.get("submission_started_at")
                     or entry.get("created_at") or "")
            try:
                return datetime.fromisoformat(str(stamp)).timestamp()
            except (ValueError, TypeError):
                return 0.0

        rows.sort(key=updated_at, reverse=True)
        return [submission_evidence(entry) for entry in rows[:max(1, min(limit, 20))]]

    def _save(self, entries: dict[str, dict[str, Any]]) -> None:
        if len(entries) > MAX_ENTRIES:  # drop the oldest finalised entries
            ordered = sorted((entry for entry in entries.values()
                              if entry.get("status") in {"approved", "rejected", "expired"}),
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

    def accepted_submissions(self, identity: dict) -> list[dict]:
        """Read accepted jobs for an exact recorded connection identity.

        Legacy cards need explicit reconciliation: a server name alone cannot
        identify the original account after connection settings change.
        """
        return [{**entry["result"], "script": entry.get("script", ""),
                 "submission_id": entry.get("id", ""),
                 "submitted_at": entry.get("submission_started_at")}
                for entry in self._load(strict=True).values()
                if entry.get("status") == "approved"
                and isinstance(entry.get("result"), dict)
                and entry["result"].get("job_id")
                and entry["result"].get("server_identity") == identity]

    @_locked
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

    @_locked
    def regenerate(self, entry_id: str, *,
                   ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict[str, Any]:
        """Issue a fresh card for an expired, otherwise unchanged submission.

        The frozen command parameters are copied into a new pending entry. The
        caller is responsible for re-validating the current remote script
        digest before exposing the new card to the user.
        """
        if not ID_RE.fullmatch(entry_id or ""):
            raise ValidationError("invalid pending-submit id")
        entries = self._load()
        self._expire(entries)
        source = entries.get(entry_id)
        if source is None:
            raise ValidationError(f"pending submit {entry_id!r} not found")
        status = str(source.get("status") or "")
        if status == "pending":
            return source
        if status != "expired":
            raise ValidationError(
                f"pending submit {entry_id} is already {status or 'settled'}")
        import secrets
        now = _utcnow()
        entry = {
            "id": f"ps-{secrets.token_urlsafe(9)}",
            "server": str(source.get("server") or ""),
            "directory": str(source.get("directory") or ""),
            "script": str(source.get("script") or ""),
            "script_sha256": str(source.get("script_sha256") or ""),
            "script_content": str(source.get("script_content") or ""),
            "session_id": str(source.get("session_id") or ""),
            "created_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(seconds=ttl_seconds)
                           ).isoformat(timespec="seconds"),
            "status": "pending",
            "regenerated_from": entry_id,
        }
        entries[entry["id"]] = entry
        self._save(entries)
        return entry

    @_locked
    def get(self, entry_id: str) -> dict[str, Any]:
        if not ID_RE.fullmatch(entry_id or ""):
            raise ValidationError("invalid pending-submit id")
        entries = self._load()
        self._expire(entries)
        entry = entries.get(entry_id)
        if entry is None:
            raise ValidationError(f"pending submit {entry_id!r} not found")
        return entry

    @_locked
    def claim(self, entry_id: str) -> dict[str, Any]:
        """Persist the one permitted submission before contacting a scheduler.

        A crash leaves ``submitting``, never a retryable approval. Both that
        state and ``uncertain`` require checking the remote scheduler first.
        """
        if not ID_RE.fullmatch(entry_id or ""):
            raise ValidationError("invalid pending-submit id")
        entries = self._load()
        self._expire(entries)
        entry = entries.get(entry_id)
        if entry is None:
            raise ValidationError(f"pending submit {entry_id!r} not found")
        if entry.get("status") != "pending":
            raise ValidationError(
                f"pending submit {entry_id} is already {entry.get('status')}; "
                "check the scheduler before submitting again")
        entry.update(status="submitting", submission_started_at=_utcnow().isoformat())
        self._save(entries)
        return entry

    @_locked
    def finish_submission(self, entry_id: str, *, result: dict | None = None,
                          error: str | None = None,
                          execution: dict | None = None) -> dict[str, Any]:
        entries = self._load()
        entry = entries.get(entry_id)
        if entry is None or entry.get("status") != "submitting":
            raise ValidationError("submission is not claimed by an active request")
        entry["status"] = "uncertain" if error is not None else "approved"
        entry["settled_at"] = _utcnow().isoformat(timespec="seconds")
        if error is not None:
            entry["error"] = str(error)[:2000]
        if result is not None:
            entry["result"] = result
        details = execution if execution is not None else (result or {}).get("submission")
        if isinstance(details, dict):
            entry["execution"] = execution_evidence(details)
        self._save(entries)
        return entry

    @_locked
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

    @_locked
    def pending(self, session_id: str = "") -> list[dict[str, Any]]:
        entries = self._load()
        self._expire(entries)
        out = [e for e in entries.values() if e.get("status") in OPEN_STATUSES]
        if session_id:
            out = [e for e in out if e.get("session_id") == session_id]
        out.sort(key=lambda e: str(e.get("created_at", "")), reverse=True)
        return out

    @_locked
    def history(self, session_id: str = "", *, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent pending/expired cards so a refreshed UI can recover."""
        entries = self._load()
        self._expire(entries)
        out = [e for e in entries.values()
               if e.get("status") in OPEN_STATUSES | {"expired"}]
        if session_id:
            out = [e for e in out if e.get("session_id") == session_id]
        out.sort(key=lambda e: str(e.get("created_at", "")), reverse=True)
        return out[:max(1, min(int(limit), 50))]


def submission_evidence(entry: dict[str, Any]) -> dict[str, Any]:
    """Separate human approval, a submit attempt, and scheduler acceptance."""
    row = {key: entry[key] for key in (
        "id", "server", "directory", "script", "status", "created_at",
        "expires_at", "submission_started_at", "settled_at") if key in entry}
    status = str(row.get("status") or "unknown")
    if status == "pending":
        try:
            if datetime.fromisoformat(str(entry.get("expires_at"))) <= _utcnow():
                status = "expired"
        except (ValueError, TypeError):
            status = "unknown"
    row["status"] = status
    attempted = bool(entry.get("submission_started_at")) or status in {
        "submitting", "uncertain", "approved"}
    result = entry.get("result")
    job_id = str(result.get("job_id") or "") if isinstance(result, dict) else ""
    row.update(
        approval_received=attempted,
        submission_attempted=attempted,
        awaiting_approval=status == "pending",
        scheduler_accepted=True if job_id else None,
        job_id=job_id,
        requires_remote_reconciliation=status in {"submitting", "uncertain"},
    )
    if entry.get("error"):
        row["error"] = str(entry["error"])[:2000]
    if isinstance(entry.get("execution"), dict):
        row["execution"] = execution_evidence(entry["execution"])
    return row


def execution_evidence(details: dict) -> dict:
    """Keep bounded execution facts, never arbitrary gateway result bodies."""
    row = {key: str(details[key])[:2000] for key in (
        "command", "scheduler", "gateway_version", "scheduler_version",
        "stage", "started_at", "finished_at", "stdout", "stderr")
        if details.get(key) is not None}
    if isinstance(details.get("returncode"), int):
        row["returncode"] = details["returncode"]
    return row

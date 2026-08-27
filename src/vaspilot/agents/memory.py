"""Conversation memory: one JSON file per chat session under ``~/.vaspilot/chat``.

The store keeps rolling user/assistant message pairs that the runtime re-
injects into later turns, so the agent remembers earlier exchanges across
page reloads and UI restarts. Chat text is private: it is never written to
the audit log, only to these files.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")
MAX_MESSAGES = 60
TITLE_SNIPPET = 40


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


class ConversationStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def _path(self, session_id: str) -> Path:
        if not SESSION_ID_RE.fullmatch(session_id or ""):
            raise ValidationError("invalid session id")
        return self.directory / f"{session_id}.json"

    # -- sessions ----------------------------------------------------------------
    def create_session(self, project: str = "", title: str = "") -> dict[str, Any]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        suffix = os.urandom(4).hex()
        session_id = f"s-{stamp}-{suffix}"
        payload = {
            "session_id": session_id,
            "project": str(project or ""),
            "title": str(title or "")[:120],
            "created_at": _now(),
            "updated_at": _now(),
            "messages": [],
        }
        _atomic_write(self._path(session_id), payload)
        return payload

    def load(self, session_id: str) -> dict[str, Any] | None:
        path = self._path(session_id)
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        data.setdefault("messages", [])
        data.setdefault("project", "")
        data.setdefault("title", "")
        return data

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload["updated_at"] = _now()
        _atomic_write(self._path(str(payload.get("session_id", ""))), payload)
        return payload

    def append(self, session_id: str, role: str, content: str) -> dict[str, Any]:
        if role not in ("user", "assistant"):
            raise ValidationError("history keeps only user/assistant entries")
        payload = self.load(session_id)
        if payload is None:
            raise ValidationError(f"session {session_id!r} not found")
        content = str(content)
        messages: list[dict[str, Any]] = payload["messages"]
        messages.append({"role": role, "content": content, "at": _now()})
        if len(messages) > MAX_MESSAGES:  # rolling window, oldest dropped
            payload["messages"] = messages[-MAX_MESSAGES:]
        if not payload.get("title") and role == "user":
            snippet = content.strip().replace("\n", " ")[:TITLE_SNIPPET]
            payload["title"] = snippet
        return self.save(payload)

    def set_project(self, session_id: str, project: str) -> dict[str, Any]:
        payload = self.load(session_id)
        if payload is None:
            raise ValidationError(f"session {session_id!r} not found")
        payload["project"] = str(project or "")
        return self.save(payload)

    def rename(self, session_id: str, title: str) -> dict[str, Any]:
        payload = self.load(session_id)
        if payload is None:
            raise ValidationError(f"session {session_id!r} not found")
        payload["title"] = str(title or "").strip()[:120]
        return self.save(payload)

    def clear(self, session_id: str) -> dict[str, Any]:
        path = self._path(session_id)
        removed = path.exists()
        if removed:
            path.unlink()
        return {"cleared": removed, "session_id": session_id}

    def list_sessions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.directory.is_dir():
            return out
        for entry in sorted(self.directory.glob("*.json"), reverse=True):
            try:
                with open(entry, "r", encoding="utf-8-sig") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or "session_id" not in data:
                continue
            out.append({
                "session_id": data.get("session_id"),
                "project": data.get("project", ""),
                "title": data.get("title", ""),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
                "message_count": len(data.get("messages") or []),
            })
        out.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return out

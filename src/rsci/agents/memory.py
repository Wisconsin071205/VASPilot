"""Conversation memory: one JSON file per chat session under ``~/.rsci/chat``.

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
CONTEXT_BUDGET_TOKENS = 12000
CONTEXT_COMPACT_THRESHOLD = 0.82


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
        data.setdefault("archived_messages", [])
        data.setdefault("project", "")
        data.setdefault("title", "")
        data.setdefault("context_summary", "")
        data.setdefault("compacted_messages", 0)
        data.setdefault("parent_session_id", "")
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

    def patch_last_assistant(self, session_id: str, content: str) -> dict[str, Any]:
        """Upsert the in-flight assistant turn so a page refresh mid-stream
        loses nothing: the growing answer is flushed periodically and the
        final call replaces it with the complete text."""
        payload = self.load(session_id)
        if payload is None:
            raise ValidationError(f"session {session_id!r} not found")
        messages: list[dict[str, Any]] = payload["messages"]
        if messages and messages[-1].get("role") == "assistant":
            messages[-1]["content"] = str(content)
        else:
            messages.append({"role": "assistant", "content": str(content),
                             "at": _now()})
            if len(messages) > MAX_MESSAGES:
                payload["messages"] = messages[-MAX_MESSAGES:]
        return self.save(payload)

    def set_project(self, session_id: str, project: str) -> dict[str, Any]:
        payload = self.load(session_id)
        if payload is None:
            raise ValidationError(f"session {session_id!r} not found")
        payload["project"] = str(project or "")
        return self.save(payload)

    def replace_project(self, old_project: str, new_project: str) -> int:
        """Retarget every saved session after a local project rename."""
        changed = 0
        for row in self.list_sessions():
            if row.get("project") != old_project:
                continue
            payload = self.load(str(row.get("session_id") or ""))
            if payload is None:
                continue
            payload["project"] = str(new_project or "")
            self.save(payload)
            changed += 1
        return changed

    def rename(self, session_id: str, title: str) -> dict[str, Any]:
        payload = self.load(session_id)
        if payload is None:
            raise ValidationError(f"session {session_id!r} not found")
        payload["title"] = str(title or "").strip()[:120]
        return self.save(payload)

    def fork(self, session_id: str, *, title: str = "") -> dict[str, Any]:
        """Create an independent branch from the current saved context."""
        source = self.load(session_id)
        if source is None:
            raise ValidationError(f"session {session_id!r} not found")
        branch = self.create_session(
            project=str(source.get("project") or ""),
            title=(str(title or "").strip() or
                   f"{str(source.get('title') or '未命名会话')} · 分支"))
        branch["messages"] = [dict(row) for row in source.get("messages") or []]
        branch["archived_messages"] = [
            dict(row) for row in source.get("archived_messages") or []]
        branch["context_summary"] = str(source.get("context_summary") or "")
        branch["compacted_messages"] = int(source.get("compacted_messages") or 0)
        branch["parent_session_id"] = session_id
        branch["branched_at"] = _now()
        return self.save(branch)

    def compact(self, session_id: str, *, keep_last: int = 8) -> dict[str, Any]:
        """Compress old turns into bounded context and retain recent messages.

        This deterministic representation is intentionally provider-independent:
        it works offline and never sends stored chat text to a second model call.
        """
        payload = self.load(session_id)
        if payload is None:
            raise ValidationError(f"session {session_id!r} not found")
        keep = max(2, min(int(keep_last), 20))
        messages = list(payload.get("messages") or [])
        old = messages[:-keep] if len(messages) > keep else []
        if not old:
            return {**payload, "compacted_now": 0}
        lines = []
        previous = str(payload.get("context_summary") or "").strip()
        if previous:
            lines.append(previous)
        for row in old:
            role = "用户" if row.get("role") == "user" else "助手"
            content = " ".join(str(row.get("content") or "").split())
            lines.append(f"- {role}: {content[:500]}")
        summary = "\n".join(lines)
        if len(summary) > 6000:
            summary = summary[-6000:]
            summary = "[更早摘要已截断]\n" + summary
        payload["context_summary"] = summary
        payload.setdefault("archived_messages", []).extend(old)
        payload["messages"] = messages[-keep:]
        payload["compacted_messages"] = int(
            payload.get("compacted_messages") or 0) + len(old)
        payload["last_compacted_at"] = _now()
        saved = self.save(payload)
        return {**saved, "compacted_now": len(old)}

    def context_stats(self, session_id: str, *, pending_text: str = "",
                      budget_tokens: int = CONTEXT_BUDGET_TOKENS) -> dict[str, Any]:
        """Return a cheap, provider-independent estimate for the context meter.

        Exact tokenization differs by model and language. Four UTF-8 Python
        characters per token is intentionally presented as an estimate; the
        meter is for early warning, while automatic compaction keeps the
        active prompt bounded before a model call.
        """
        payload = self.load(session_id)
        if payload is None:
            raise ValidationError(f"session {session_id!r} not found")
        active = list(payload.get("messages") or [])
        archived = list(payload.get("archived_messages") or [])
        summary = str(payload.get("context_summary") or "")
        pending = str(pending_text or "")
        chars = sum(len(str(row.get("content") or "")) for row in active)
        chars += len(summary) + len(pending)
        budget = max(1000, int(budget_tokens))
        estimated = (chars + 3) // 4
        percent = min(100, round(estimated * 100 / budget, 1))
        threshold = round(budget * CONTEXT_COMPACT_THRESHOLD)
        return {
            "estimated_tokens": estimated,
            "budget_tokens": budget,
            "percent": percent,
            "estimated_chars": chars,
            "active_messages": len(active),
            "archived_messages": len(archived),
            "compacted_messages": int(payload.get("compacted_messages") or 0),
            "has_summary": bool(summary),
            "pending_chars": len(pending),
            "compact_threshold_tokens": threshold,
            "needs_compaction": estimated >= threshold,
            "can_compact": len(active) > 2,
        }

    def clear(self, session_id: str) -> dict[str, Any]:
        path = self._path(session_id)
        removed = path.exists()
        if removed:
            path.unlink()
        return {"cleared": removed, "session_id": session_id}

    def delete(self, session_id: str) -> dict[str, Any]:
        """Permanently delete one conversation file.

        Keep this separate from :meth:`clear`: clearing keeps the session
        shell, while deletion removes the session from every future listing.
        ``_path`` performs session-id validation before filesystem access.
        """
        path = self._path(session_id)
        deleted = path.exists()
        if deleted:
            path.unlink()
        return {"deleted": deleted, "session_id": session_id}

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
                "message_count": (len(data.get("archived_messages") or [])
                                  + len(data.get("messages") or [])),
                "compacted_messages": int(data.get("compacted_messages") or 0),
                "has_summary": bool(data.get("context_summary")),
                "parent_session_id": data.get("parent_session_id", ""),
            })
        out.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return out

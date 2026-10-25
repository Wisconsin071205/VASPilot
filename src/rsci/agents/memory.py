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
import threading
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError
from ..core.locking import exclusive_file_lock

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")
MAX_MESSAGES = 60
TITLE_SNIPPET = 40
CONTEXT_BUDGET_TOKENS = 12000
CONTEXT_COMPACT_THRESHOLD = 0.82
_HELD = threading.local()


def _session_locked(method):
    """Serialize the complete read/modify/write, including nested store calls."""
    @wraps(method)
    def locked(self, session, *args, **kwargs):
        session_id = session.get("session_id", "") if isinstance(session, dict) else session
        path = self._path(session_id).with_suffix(".lock")
        key = str(path.resolve())
        held = getattr(_HELD, "paths", set())
        if key in held:
            return method(self, session, *args, **kwargs)
        with exclusive_file_lock(path):
            _HELD.paths = held | {key}
            try:
                return method(self, session, *args, **kwargs)
            finally:
                _HELD.paths = held
    return locked


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

    def execution_path(self, session_id: str) -> Path:
        return self._path(session_id).with_suffix(".turn.lock")

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

    @_session_locked
    def load(self, session_id: str) -> dict[str, Any] | None:
        path = self._path(session_id)
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("session_id") != session_id:
            return None
        data.setdefault("messages", [])
        data.setdefault("archived_messages", [])
        data.setdefault("project", "")
        data.setdefault("title", "")
        data.setdefault("context_summary", "")
        data.setdefault("compacted_messages", 0)
        data.setdefault("parent_session_id", "")
        data.setdefault("workflow_plans", [])
        if not isinstance(data["workflow_plans"], list) or any(
                not isinstance(pid, str) or not re.fullmatch(r"[0-9a-f]{16}", pid)
                for pid in data["workflow_plans"]):
            return None
        for field in ("messages", "archived_messages"):
            if not isinstance(data[field], list) or any(
                    not isinstance(row, dict) or row.get("role") not in ("user", "assistant")
                    or not isinstance(row.get("content"), str) for row in data[field]):
                return None
        if not isinstance(data.get("turn", {}), dict):
            return None
        return data

    @_session_locked
    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload["updated_at"] = _now()
        _atomic_write(self._path(str(payload.get("session_id", ""))), payload)
        return payload

    @_session_locked
    def append(self, session_id: str, role: str, content: str) -> dict[str, Any]:
        if role not in ("user", "assistant"):
            raise ValidationError("history keeps only user/assistant entries")
        payload = self.load(session_id)
        if payload is None:
            raise ValidationError(f"session {session_id!r} not found")
        content = str(content)
        messages: list[dict[str, Any]] = payload["messages"]
        messages.append({"role": role, "content": content, "at": _now()})
        if len(messages) > MAX_MESSAGES:
            self._archive(payload, MAX_MESSAGES)
        if not payload.get("title") and role == "user":
            snippet = content.strip().replace("\n", " ")[:TITLE_SNIPPET]
            payload["title"] = snippet
        return self.save(payload)

    @_session_locked
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
                self._archive(payload, MAX_MESSAGES)
        return self.save(payload)

    @_session_locked
    def set_project(self, session_id: str, project: str) -> dict[str, Any]:
        payload = self.load(session_id)
        if payload is None:
            raise ValidationError(f"session {session_id!r} not found")
        payload["project"] = str(project or "")
        return self.save(payload)

    @_session_locked
    def bind_workflow_plan(self, session_id: str, plan_id: str) -> dict[str, Any]:
        """Persist only a plan reference, never an approval token or tool payload."""
        if not re.fullmatch(r"[0-9a-f]{16}", plan_id or ""):
            raise ValidationError("invalid plan id")
        payload = self.load(session_id)
        if payload is None:
            raise ValidationError("会话不存在或记录损坏，无法关联计划")
        if plan_id not in payload["workflow_plans"]:
            payload["workflow_plans"].append(plan_id)
            return self.save(payload)
        return payload

    def replace_project(self, old_project: str, new_project: str) -> int:
        """Retarget every saved session after a local project rename."""
        changed = 0
        for row in self.list_sessions():
            if row.get("project") != old_project:
                continue
            payload = self.load(str(row.get("session_id") or ""))
            if payload is None:
                continue
            self.set_project(str(row["session_id"]), new_project)
            changed += 1
        return changed

    @_session_locked
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
        branch["workflow_plans"] = list(source.get("workflow_plans", []))
        branch["branched_at"] = _now()
        return self.save(branch)

    @_session_locked
    def compact(self, session_id: str, *, keep_last: int = 8) -> dict[str, Any]:
        """Compress old turns into bounded context and retain recent messages.

        This deterministic representation is intentionally provider-independent:
        it works offline and never sends stored chat text to a second model call.
        """
        payload = self.load(session_id)
        if payload is None:
            raise ValidationError(f"session {session_id!r} not found")
        keep = max(2, min(int(keep_last), 20))
        count = self._archive(payload, keep)
        return {**self.save(payload), "compacted_now": count}

    @staticmethod
    def _archive(payload: dict, keep: int) -> int:
        messages = list(payload.get("messages") or [])
        old = messages[:-keep] if len(messages) > keep else []
        if not old:
            return 0
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
        return len(old)

    @_session_locked
    def begin_turn(self, session_id: str, request_id: str, content: str,
                   *, provider: str = "") -> dict:
        payload = self.load(session_id)
        if payload is None:
            raise ValidationError("会话不存在或记录损坏，请新建会话；原文件已保留")
        rows = payload["archived_messages"] + payload["messages"]
        if any(row.get("request_id") == request_id for row in rows):
            raise ValidationError("这条请求已经接收，请查看原回答")
        # The caller owns the execution lock. A previous running record is
        # therefore orphaned, even if this client did not first load the UI.
        previous = payload.get("turn", {})
        if previous.get("status") == "running":
            for row in rows:
                if row.get("role") == "assistant" and row.get("request_id") == previous.get("request_id"):
                    row.update(status="interrupted", error="此回答因应用中断而未完成；请核对已执行操作。")
        payload["messages"].extend([
            {"role": "user", "content": content, "at": _now(), "request_id": request_id},
            {"role": "assistant", "content": "", "at": _now(),
             "request_id": request_id, "status": "running"},
        ])
        if not payload["title"]:
            payload["title"] = content.strip().replace("\n", " ")[:TITLE_SNIPPET]
        if len(payload["messages"]) > MAX_MESSAGES:
            self._archive(payload, MAX_MESSAGES)
        payload["turn"] = {"request_id": request_id, "status": "running",
                           "stage": "preparing", "provider": provider,
                           "started_at": _now(), "updated_at": _now(), "tools": []}
        return self.save(payload)

    @_session_locked
    def update_turn(self, session_id: str, request_id: str, *, answer: str | None = None,
                    **changes) -> dict:
        payload = self.load(session_id)
        if payload is None or payload.get("turn", {}).get("request_id") != request_id:
            raise ValidationError("会话执行记录已改变，无法保存到其他回答")
        turn = payload["turn"]
        turn.update(changes, updated_at=_now())
        for row in reversed(payload["archived_messages"] + payload["messages"]):
            if row.get("role") == "assistant" and row.get("request_id") == request_id:
                if answer is not None:
                    row["content"] = str(answer)
                row["status"] = turn["status"]
                if changes.get("error"):
                    row["error"] = str(changes["error"])
                break
        return self.save(payload)

    def snapshot(self, session_id: str) -> dict:
        """A vanished owner interrupts a turn; reading never replays its tools."""
        from ..core.locking import try_file_lock
        with try_file_lock(self.execution_path(session_id)) as idle:
            payload = self.load(session_id)
            if payload is None:
                raise ValidationError("会话不存在或记录损坏，原文件已保留")
            turn = payload.get("turn", {})
            if idle and turn.get("status") == "running":
                payload = self.update_turn(
                    session_id, turn["request_id"], status="interrupted", stage="finished",
                    error="应用已中断此回答。已保存的内容仍可查看，请核对已执行操作后继续。")
                turn = payload["turn"]
            return {**payload, "turn": {**turn, "active": not idle}}

    def export_markdown(self, session_id: str) -> dict:
        payload = self.load(session_id)
        if payload is None:
            raise ValidationError("会话不存在或记录损坏")
        parts = ["# " + str(payload["title"] or "未命名会话"),
                 f"会话：{session_id}", f"项目：{payload['project'] or '未绑定'}"]
        for row in payload["archived_messages"] + payload["messages"]:
            parts.extend(["## " + ("用户" if row["role"] == "user" else "助手"), row["content"]])
            if row.get("error"):
                parts.append("状态：" + str(row["error"]))
        return {"filename": f"{session_id}.md", "content": "\n\n".join(parts) + "\n"}

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

    @_session_locked
    def clear(self, session_id: str) -> dict[str, Any]:
        path = self._path(session_id)
        removed = path.exists()
        if removed:
            path.unlink()
        return {"cleared": removed, "session_id": session_id}

    @_session_locked
    def delete(self, session_id: str) -> dict[str, Any]:
        """Permanently delete one conversation file.

        Both this and the legacy clear endpoint remove the session file.
        ``_path`` performs session-id validation before filesystem access.
        """
        path = self._path(session_id)
        deleted = path.exists()
        if deleted:
            path.unlink()
        return {"deleted": deleted, "session_id": session_id}

    def list_sessions(self, *, query: str = "") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.directory.is_dir():
            return out
        for entry in sorted(self.directory.glob("*.json"), reverse=True):
            try:
                data = self.load(entry.stem)
            except (OSError, ValueError, ValidationError):
                continue
            if not isinstance(data, dict) or "session_id" not in data:
                continue
            needle = query.strip().casefold()
            if needle and not any(needle in str(value).casefold() for value in [
                    data.get("title", ""), data.get("project", ""),
                    *(row["content"] for row in data["archived_messages"] + data["messages"])]):
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
                "turn_status": (data.get("turn") or {}).get("status", ""),
            })
        out.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return out

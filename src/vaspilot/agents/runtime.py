"""Agent runtime: the provider-agnostic tool-calling loop.

The runtime owns the safety envelope:
  - providers that failed any capability probe run in ``analysis_only`` mode
    and may only dispatch read tools (enforced by the registry)
  - the loop is turn-bounded; runaway tool chatter terminates with an error
  - every tool call and its outcome lands in the audit log (sanitized)
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..core.audit import AuditLog
from ..core.errors import ProviderError, ToolNotAllowedError
from ..providers.base import ANALYSIS_ONLY, BaseProvider
from ..tools.registry import ToolRegistry

SYSTEM_PROMPT = """You are VASPilot's agent operating a restricted VASP/HPC
environment through a named tool registry. Core rules:

- You have NO shell. Every remote action is one named tool call with
  constrained parameters; never try bash, sh, powershell or command strings.
- All remote paths must be absolute and inside the target server's root.
- Scheduler COMPLETED never implies scientific convergence; check
  vasp_progress for convergence and say which dimension you are reporting.
- POTCAR content is never readable; only its metadata is.
- Destructive operations (trash/restore/purge, submit, cancel) are gated:
  they either need a human-issued approval reference or explicit local
  confirmation. Ask the user to run 'vaspilot workflow approve' themselves.
- When you finish, answer concisely with the facts you observed.
"""


class AgentRuntime:
    def __init__(self, *, provider: BaseProvider, registry: ToolRegistry,
                 mode: str, audit: AuditLog | None = None,
                 max_turns: int = 12,
                 stream_cb: Callable[[str], None] | None = None) -> None:
        self.provider = provider
        self.registry = registry
        self.mode = mode  # "full" | "analysis_only"
        self.audit = audit
        self.max_turns = max_turns
        self.stream_cb = stream_cb

    # ---------------------------------------------------------------- run
    def run(self, goal: str) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": goal},
        ]
        tools = [t.to_openai() for t in self.registry.list_tools()]
        trace: list[dict[str, Any]] = []
        for turn in range(1, self.max_turns + 1):
            reply = self.provider.chat(messages, tools,
                                       stream_cb=self.stream_cb)
            if reply.tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": reply.text or "",
                    "tool_calls": [
                        {"id": call.call_id, "type": "function",
                         "function": {"name": call.name,
                                      "arguments": json.dumps(
                                          call.arguments, ensure_ascii=False)}}
                        for call in reply.tool_calls],
                })
                for call in reply.tool_calls:
                    outcome = self._dispatch(call.name, call.arguments)
                    trace.append({"turn": turn, "tool": call.name,
                                  "ok": bool(outcome.get("ok", True))})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": json.dumps(outcome, ensure_ascii=False,
                                              default=str)[:20000],
                    })
                continue
            return {
                "ok": True,
                "answer": reply.text,
                "turns": turn,
                "tool_calls": [row["tool"] for row in trace],
                "trace": trace,
                "mode": self.mode,
            }
        return {
            "ok": False,
            "error": f"agent exceeded {self.max_turns} turns without finishing",
            "turns": self.max_turns,
            "tool_calls": [row["tool"] for row in trace],
            "trace": trace,
            "mode": self.mode,
        }

    # ---------------------------------------------------------------- chat
    def chat(self, user_text: str, history: list[dict[str, Any]] | None = None
             ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT}]
        for row in history or []:
            if isinstance(row, dict) and row.get("role") in ("user", "assistant"):
                messages.append({"role": row["role"],
                                 "content": str(row.get("content", ""))})
        messages.append({"role": "user", "content": user_text})
        tools = [t.to_openai() for t in self.registry.list_tools()]
        reply = self.provider.chat(messages, tools, stream_cb=self.stream_cb)
        if reply.tool_calls:
            return {
                "ok": True,
                "answer": reply.text or
                "(the model proposed tool calls; use 'agent run' for "
                "goal-driven execution)",
                "proposed_tools": [call.name for call in reply.tool_calls],
                "mode": self.mode,
            }
        return {"ok": True, "answer": reply.text, "mode": self.mode}

    # ------------------------------------------------------------ dispatch
    def _dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            outcome = self.registry.dispatch(name, arguments,
                                             provider_mode=self.mode)
            if self.audit:
                self.audit.record("agent.tool", outcome="ok", tool=name)
            return outcome if isinstance(outcome, dict) else {"ok": True}
        except ToolNotAllowedError as exc:
            if self.audit:
                self.audit.record("agent.tool", outcome="blocked", tool=name,
                                  error=exc.to_dict())
            return {"ok": False, "error": exc.to_dict(),
                    "hint": "this provider is analysis_only; ask the user to "
                            "run this operation locally or re-probe the provider"}
        except Exception as exc:  # tool errors become model-visible results
            if self.audit:
                self.audit.record("agent.tool", outcome="failed", tool=name,
                                  error={"message": str(exc)[:200]})
            return {"ok": False, "error": {"code": "tool_error",
                                           "message": str(exc)[:500]}}

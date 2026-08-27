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

SYSTEM_PROMPT = """You are VASPilot's agent operating a VASP/HPC environment
through a named, audited tool registry. Core rules:

- Prefer the named tools (remote_*, project_*, job_*, vasp_*) over the raw
  shell tools; shell_run / remote_run exist for everything the named tools
  cannot do. Every command you run is fully audited.
- All remote paths in named tools must be absolute and inside the target
  server's root.
- Scheduler COMPLETED never implies scientific convergence; check
  vasp_progress for convergence and say which dimension you are reporting.
- Before the FIRST submission to any cluster, probe its queues/partitions
  (PBS: qstat -q — always set '#PBS -q'; Slurm: sinfo — pick an explicit
  '#SBATCH --partition' when several exist or confirm the *default). Tell
  the user which queue/partition a job will use and why.
- POTCAR content is never readable or writable; only its metadata is.
  POTCAR libraries usually live ON the HPC side. When the project records
  a remote POTCAR path, or the user points at one, assemble it directly on
  the server with ONE quiet audited remote_run command (copy/concatenate
  into the run directory) right before submitting — never cat it into the
  conversation and never ask the user to paste its content.
- job_submit normally pauses for human confirmation: tell the user to click
  the approval card in the web UI, then continue in the next turn. If an
  approval_ref is required, the user runs 'vaspilot workflow approve'.
- The full calculation loop is yours to drive: analyze the structure,
  project_create + project_write the INCAR/KPOINTS/POSCAR (and a custom
  run.job.sh when useful), project_validate, remote_mkdir + upload_file,
  job_submit, poll job_state/vasp_progress, download_file the results and
  interpret them. Multi-step chains (relax -> SCF -> DOS/NEB/charge/levels)
  are done by repeating the loop per stage and carrying CHGCAR/CONTCAR
  between stages.
- Earlier turns of this conversation are provided to you automatically;
  rely on that memory instead of asking the user to repeat themselves.
- remote_run is a PERSISTENT terminal per server: the working directory
  from your previous command is still in effect (the result's `cwd` field
  shows where you are); plain `cd x` switches it for later commands, and
  reset:true returns home. Environment stacking (module load / conda
  activate) must still be chained inline on the same command line.
- Fetching result files to THIS computer is download_file; remote_copy only
  moves data between two paths ON the same server.
- web_search/web_fetch may look up literature values or error fixes;
  server_metrics shows load and idle resources before you pick a server.
- When you finish, answer concisely with the facts you observed.
"""


def filtered_history(history: list[dict[str, Any]] | None
                     ) -> list[dict[str, Any]]:
    """Keep only plain user/assistant text turns for re-injection."""
    out: list[dict[str, Any]] = []
    for row in history or []:
        if isinstance(row, dict) and row.get("role") in ("user", "assistant"):
            out.append({"role": row["role"],
                        "content": str(row.get("content", ""))})
    return out


def _bounded(outcome: dict[str, Any], limit: int = 4000) -> dict[str, Any]:
    """Keep event payloads small enough for UI transport."""
    try:
        import json as _json
        text = _json.dumps(outcome, ensure_ascii=False, default=str)
        if len(text) <= limit:
            return outcome
        return {"ok": bool(outcome.get("ok", True)),
                "truncated": True,
                "preview": text[:limit] + "…"}
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return {"ok": False, "preview": str(outcome)[:limit]}


class AgentRuntime:
    def __init__(self, *, provider: BaseProvider, registry: ToolRegistry,
                 mode: str, audit: AuditLog | None = None,
                 max_turns: int = 12,
                 stream_cb: Callable[[str], None] | None = None,
                 event_cb: Callable[[str, dict[str, Any]], None] | None = None,
                 system_extra: str = "") -> None:
        self.provider = provider
        self.registry = registry
        self.mode = mode  # "full" | "analysis_only"
        self.audit = audit
        self.max_turns = max_turns
        self.stream_cb = stream_cb
        # event_cb(kind, payload) lets hosts (local web UI) observe the loop:
        # kinds: "tool" (name, ok), "final" (result), "error" (message)
        self.event_cb = event_cb
        # hosts append e.g. the skill index or the active project context
        self.system_prompt = SYSTEM_PROMPT + (
            "\n\n" + system_extra if system_extra else "")

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.event_cb is not None:
            try:
                self.event_cb(kind, payload)
            except Exception:  # UI piping must never break the agent loop
                pass

    # ---------------------------------------------------------------- run
    def run(self, goal: str,
            history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            *filtered_history(history),
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
                    row = {"turn": turn, "tool": call.name,
                           "ok": bool(outcome.get("ok", True))}
                    trace.append(row)
                    self._emit("tool", {**row,
                                        "arguments": call.arguments,
                                        "outcome": _bounded(outcome)})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": json.dumps(outcome, ensure_ascii=False,
                                              default=str)[:20000],
                    })
                continue
            result = {
                "ok": True,
                "answer": reply.text,
                "turns": turn,
                "tool_calls": [row["tool"] for row in trace],
                "trace": trace,
                "mode": self.mode,
            }
            self._emit("final", result)
            return result
        result = {
            "ok": False,
            "error": f"agent exceeded {self.max_turns} turns without finishing",
            "turns": self.max_turns,
            "tool_calls": [row["tool"] for row in trace],
            "trace": trace,
            "mode": self.mode,
        }
        self._emit("final", result)
        return result

    # ---------------------------------------------------------------- chat
    def chat(self, user_text: str, history: list[dict[str, Any]] | None = None
             ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            *filtered_history(history),
            {"role": "user", "content": user_text},
        ]
        tools = [t.to_openai() for t in self.registry.list_tools()]
        reply = self.provider.chat(messages, tools, stream_cb=self.stream_cb)
        if reply.tool_calls:
            result = {
                "ok": True,
                "answer": reply.text or
                "(the model proposed tool calls; use 'agent run' for "
                "goal-driven execution)",
                "proposed_tools": [call.name for call in reply.tool_calls],
                "mode": self.mode,
            }
        else:
            result = {"ok": True, "answer": reply.text, "mode": self.mode}
        self._emit("final", result)
        return result

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

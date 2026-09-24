"""Agent runtime: the provider-agnostic tool-calling loop.

The runtime owns the safety envelope:
  - providers that failed any capability probe run in ``analysis_only`` mode
    and may only dispatch read tools (enforced by the registry)
  - the loop runs until the model answers or the user stops it; scripted
    callers may set an explicit tool-turn limit
  - every tool call and its outcome lands in the audit log (sanitized)
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..core.audit import AuditLog
from ..core.errors import ProviderError, ToolNotAllowedError
from ..providers.base import ANALYSIS_ONLY, BaseProvider
from ..tools.registry import ToolRegistry

SYSTEM_PROMPT = """You are rsci's agent operating a VASP/HPC environment
through a named, audited tool registry. Core rules:

- Prefer the named tools (remote_*, project_*, job_*, vasp_*) over the raw
  shell tools; shell_run / remote_run exist for everything the named tools
  cannot do. Every command you run is fully audited.
- Creating a project only requires a name; an empty project is a valid draft.
  Do not require INCAR/KPOINTS/POSCAR or POTCAR during project creation.
  The web UI's calculation settings edit the SAME local project files as
  project_read/project_write. When asked to prepare or adjust a calculation,
  read the current files first, preserve unrelated user changes, and write
  the actual INCAR/KPOINTS/POSCAR and optional run.job.sh using project_write.
  Do not only suggest file contents in chat. Ask for missing scientific
  requirements or a real structure instead of inventing them. Validate the
  saved inputs with project_validate before workflow_prepare. Changed inputs
  require a new execution plan and approval; never reuse a stale plan.
- All remote paths in named tools must be absolute and inside the target
  server's root.
- Scheduler COMPLETED never implies scientific convergence; check
  vasp_progress for convergence and say which dimension you are reporting.
- Before the FIRST submission to any cluster, probe its queues/partitions
  (PBS: qstat -q — always set '#PBS -q'; Slurm: sinfo — pick an explicit
  '#SBATCH --partition' when several exist or confirm the *default). Tell
  the user which queue/partition a job will use and why.
- POTCAR content is never readable or writable; only its metadata is.
  POTCAR libraries usually live ON the HPC side. The single-stage workflow
  does not assemble them: if POTCAR is skipped locally, the remote run
  directory must already contain the correct assembled POTCAR before the
  plan is validated. If it is absent, ask the user to prepare it with a
  separate audited action; never print its content or ask the user to paste it.
- job_submit normally pauses for human confirmation: tell the user to click
  the approval card in the web UI, then continue in the next turn. If an
  approval_ref is required, the user runs 'rsci workflow approve'.
- For a new calculation from the active local project, prepare the
  authoritative execution plan with workflow_prepare after the input files
  are ready. This is mandatory for a standard local-input run: do not call
  remote_mkdir, upload_file or job_submit for that same run before approval.
  The tool pauses at a human approval card; after it returns, stop and wait
  for the user instead of continuing with write tools. Use workflow_status on
  a later turn to report the persisted run. The direct loop below is for
  ad-hoc repair/diagnostic work or a run that needs an HPC-side POTCAR
  assembly the immutable plan cannot represent.
- The calculation loop is a single-stage submission: prepare one complete
  INCAR/KPOINTS/POSCAR/POTCAR input set, call workflow_prepare, wait for the
  human approval card, and let the workflow engine perform mkdir, upload,
  validation, submission, monitoring, download and parsing. Do not claim
  that relax -> SCF -> DOS/NEB/charge/levels chaining is implemented.
- When the user provides a VESTA .vasp file with requested calculations
  annotated below the coordinates, use the VASPKIT campaign flow: call
  campaign_plan without a recipe to inspect the structure and annotation,
  build the recipe only from the user's request (ask about ambiguity; never
  invent a Hubbard U), call campaign_plan again to show the stages, then
  campaign_start. Run vaspkit_doctor once before the first campaign on a
  server. The campaign runner advances the stage chain; report progress with
  campaign_status.
- During tool-driven work, call the needed tools directly instead of
  narrating every lookup. Keep the user-visible response for the final
  conclusion after the tools have returned.
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
                 max_turns: int | None = None,
                 stream_cb: Callable[[str], None] | None = None,
                 event_cb: Callable[[str, dict[str, Any]], None] | None = None,
                 system_extra: str = "", cancel: Any = None,
                 cancel_event: Any = None) -> None:
        self.provider = provider
        self.registry = registry
        self.mode = mode  # "full" | "analysis_only"
        self.audit = audit
        # No turn budget by default. A positive max_turns is available only
        # for callers that explicitly request a bound.
        self.max_turns = int(max_turns) if max_turns and int(max_turns) > 0 \
            else None
        self.cancel = cancel if cancel is not None else cancel_event
        self.stream_cb = stream_cb
        # event_cb(kind, payload) lets hosts (local web UI) observe the loop:
        # kinds: "tool" (name, ok), "final" (result), "error" (message)
        self.event_cb = event_cb
        # hosts append e.g. the skill index or the active project context
        self.system_prompt = SYSTEM_PROMPT + (
            "\n\n" + system_extra if system_extra else "")

    def _check_cancelled(self) -> None:
        if self._cancelled():
            raise InterruptedError("agent response cancelled by user")

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.event_cb is not None:
            try:
                self.event_cb(kind, payload)
            except Exception:  # UI piping must never break the agent loop
                pass

    def _provider_chat(self, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]]):
        """Run one model turn without leaking tool-turn preambles.

        OpenAI-compatible streams can contain assistant text before the
        model emits a tool call (for example, "let me check...").  The
        runtime cannot know that text is intermediate until the stream has
        ended, so buffer it here.  Only text from a turn with no tool calls
        is forwarded to the user-facing stream callback.
        """
        if self.stream_cb is None:
            return self.provider.chat(messages, tools)
        buffered: list[str] = []

        def capture(fragment: str) -> None:
            # Keep cancellation responsive even while output is being held
            # back until we know whether this turn contains tool calls.
            self._check_cancelled()
            buffered.append(fragment)

        reply = self.provider.chat(messages, tools,
                                   stream_cb=capture)
        if not reply.tool_calls:
            for fragment in buffered:
                self.stream_cb(fragment)
        return reply

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
        turn = 0
        while True:
            if self._cancelled():
                return self._stopped(turn, trace)
            if self.max_turns is not None and turn >= self.max_turns:
                result = {
                    "ok": False,
                    "error": f"agent used the {self.max_turns} tool turns "
                             "this caller allowed",
                    "turns": turn,
                    "tool_calls": [row["tool"] for row in trace],
                    "trace": trace,
                    "mode": self.mode,
                }
                self._emit("error", result)
                return result
            turn += 1
            reply = self._provider_chat(messages, tools)
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
                    if self._cancelled():
                        return self._stopped(turn, trace, reply.text)
                    outcome = self._dispatch(call.name, call.arguments)
                    row = {"turn": turn, "tool": call.name,
                           "ok": bool(outcome.get("ok", True))}
                    trace.append(row)
                    event_outcome = _bounded(outcome)
                    # Keep the compact approval-card payload even when the
                    # model-visible tool result is too large for the SSE cap.
                    if outcome.get("chat_plan"):
                        event_outcome["chat_plan"] = outcome["chat_plan"]
                    self._emit("tool", {**row,
                                        "arguments": call.arguments,
                                        "outcome": event_outcome})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": json.dumps(outcome, ensure_ascii=False,
                                              default=str)[:20000],
                    })
                    if outcome.get("requires_user_action"):
                        # A workflow plan is a durable handoff to the human
                        # approval UI.  Do not give the model another write
                        # turn in which it could bypass the plan.
                        result = {
                            "ok": True,
                            "answer": str(outcome.get("message") or
                                           "The workflow plan is waiting for "
                                           "human approval."),
                            "turns": turn,
                            "tool_calls": [row["tool"] for row in trace],
                            "trace": trace,
                            "mode": self.mode,
                        }
                        self._emit("final", result)
                        return result
                continue
            self._check_cancelled()
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

    def _cancelled(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()

    def _stopped(self, turn: int, trace: list[dict[str, Any]],
                 text: str = "") -> dict[str, Any]:
        """The user pressed stop: end cleanly after the step in flight."""
        result = {
            "ok": False,
            "stopped": True,
            "answer": text or "",
            "error": "stopped by the user",
            "turns": turn,
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
        reply = self._provider_chat(messages, tools)
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

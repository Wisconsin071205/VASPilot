"""Workflow engine: preview, prepare, approve, run, resume, status.

Execution model ("immutable plan, one approval, unattended inside the plan"):

  preview    build a deterministic plan; no server contact
  prepare    build + persist the plan under <config>/plans/<plan_id>.json
  approve    LOCAL interactive confirmation mints an HMAC approval bound to
             (server, plan_hash, files_hash); the model never sees the key
  run        verify approval -> execute steps unattended; every step result
             is journaled into the run state; failures append a new attempt
  resume     continue the pending steps in a NEW attempt while the approval
             stays valid and the plan hash still matches
  status     replay the persisted run state

The scheduler-completed and scientific-converged dimensions are recorded
separately everywhere; a scheduler COMPLETED job that did not converge ends
in ``needs_review`` rather than ``completed``.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..core.audit import AuditLog
from ..core.config import Config, _atomic_write_json
from ..core.errors import ApprovalError, ValidationError, rsciError
from ..core.hashing import file_sha256, obj_sha256, text_sha256
from ..core.locking import exclusive_file_lock
from ..core.validation import valid_filename, valid_server_name
from ..gateway.client import GatewayClient
from ..hpc.vasp import scientific_status
from .approval import (DEFAULT_VALIDITY_HOURS, ApprovalToken, decode_token,
                       issue_token, verify_token)
from .plan import (DEFAULT_DOWNLOAD_FILES, build_plan, plan_files_hash,
                   verify_plan_integrity)

TERMINAL_SCHEDULER_STATES = {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED",
                             "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED"}
MONITOR_POLL_SECONDS = 60
MAX_MONITOR_POLLS = 100_000


class WorkflowEngine:
    def __init__(self, *, config: Config, client: GatewayClient,
                 audit: AuditLog | None = None,
                 monitor_poll_seconds: int = MONITOR_POLL_SECONDS,
                 clock: Callable[[], float] | None = None) -> None:
        self.config = config
        self.client = client
        self.audit = audit
        self.monitor_poll_seconds = monitor_poll_seconds
        # clock is injectable for tests; production uses time.monotonic
        self._clock = clock

    # ------------------------------------------------------------- paths
    @property
    def plans_dir(self) -> Path:
        return self.config.home / "plans"

    def plan_path(self, plan_id: str) -> Path:
        _validate_plan_id(plan_id)
        return self.plans_dir / f"{plan_id}.json"

    def run_path(self, plan_id: str) -> Path:
        _validate_plan_id(plan_id)
        return self.config.runs_dir / f"{plan_id}.json"

    # ------------------------------------------------------ preview / prepare
    def preview(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Deterministically build a plan from a validated spec dict."""
        for key in ("from_dir", "server", "remote_dir"):
            if not spec.get(key):
                raise ValidationError(f"plan spec is missing {key!r}")
        valid_server_name(str(spec["server"]))
        plan = build_plan(
            from_dir=str(spec["from_dir"]),
            server=str(spec["server"]),
            remote_dir=str(spec["remote_dir"]),
            scheduler=str(spec.get("scheduler")
                          or self._scheduler_for(str(spec["server"]))),
            job_name=str(spec.get("job_name") or "rsci"),
            partition=str(spec.get("partition") or ""),
            ntasks=int(spec.get("ntasks") or 8),
            walltime=str(spec.get("walltime") or "24:00:00"),
            vasp_executable=str(spec.get("vasp_executable") or "vasp_std"),
            download_files=tuple(spec.get("download_files")
                                 or DEFAULT_DOWNLOAD_FILES),
            monitor_timeout_min=int(spec.get("monitor_timeout_min") or 2880),
            skip_potcar=bool(spec.get("skip_potcar") or False),
        )
        return {
            "plan": plan,
            "files_hash": plan_files_hash(plan),
            "next": "review the plan, then run 'rsci workflow approve'",
        }

    def prepare(self, spec: dict[str, Any]) -> dict[str, Any]:
        preview = self.preview(spec)
        plan = preview["plan"]
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        path = self.plan_path(plan["plan_id"])
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("plan_hash") != plan["plan_hash"]:
                raise ValidationError(
                    "a different plan already exists under this plan_id")
        else:
            _atomic_write_json(path, plan)
        if self.audit:
            self.audit.record("workflow.prepare", outcome="ok",
                              plan_id=plan["plan_id"],
                              plan_hash=plan["plan_hash"][:16])
        return {"plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"],
                "files_hash": preview["files_hash"],
                "plan_file": str(path), "plan": plan}

    def load_plan(self, plan_id: str) -> dict[str, Any]:
        path = self.plan_path(plan_id)
        if not path.is_file():
            raise ValidationError(
                f"plan {plan_id} was not found; run prepare first")
        plan = json.loads(path.read_text(encoding="utf-8"))
        verify_plan_integrity(plan)
        return plan

    def _scheduler_for(self, server: str) -> str:
        entry = self.client.server_entry(server)
        return entry.scheduler if entry.scheduler in ("slurm", "pbs") else "slurm"

    # ------------------------------------------------------------- approve
    def approve(self, plan_id: str, *,
                validity_hours: int = DEFAULT_VALIDITY_HOURS,
                confirmed: bool = False,
                stdin_lines: list[str] | None = None) -> dict[str, Any]:
        """Mint an approval after LOCAL interactive confirmation.

        The confirmation phrase embeds the plan_id: typing it proves a human
        reviewed the preview in this trusted terminal. A model process that
        merely calls this function still needs the typed phrase.
        """
        plan = self.load_plan(plan_id)
        files_hash = plan_files_hash(plan)
        phrase = f"approve {plan_id}"
        if not confirmed:
            if stdin_lines is not None:
                typed = stdin_lines[0].strip() if stdin_lines else ""
            else:
                import sys
                if not sys.stdin.isatty():
                    raise ApprovalError(
                        "approval requires an interactive terminal; review "
                        "the preview, then run approve in a real terminal")
                print(f"Plan {plan_id} on server {plan['server']}")
                print(f"  remote dir : {plan['remote_dir']}")
                print(f"  files      : {len(plan['files'])} "
                      f"(files_hash {files_hash[:12]}…)")
                print(f"  submit     : run.job.sh via "
                      f"{plan['job_script']['scheduler']}")
                print(f"  risk       : {plan['risk_summary']['level']} "
                      f"(walltime {plan['job_script']['walltime']}, "
                      f"ntasks {plan['job_script']['ntasks']})")
                typed = input(f"Type '{phrase}' to approve: ").strip()
            if typed != phrase:
                raise ApprovalError(
                    "confirmation phrase did not match; approval refused")
        key = self.config.approval_signing_key()
        token = issue_token(
            key, server=plan["server"], plan_hash=plan["plan_hash"],
            files_hash=files_hash, action="workflow_run",
            parameter_hash=self._run_parameters_hash(plan),
            validity_hours=validity_hours)
        self._record_approval(token, plan_id)
        if self.audit:
            self.audit.record("workflow.approve", outcome="ok",
                              plan_id=plan_id, token_id=token.token_id,
                              validity_hours=validity_hours)
        return {
            "plan_id": plan_id,
            "approval_ref": token.encode(),
            "token_id": token.token_id,
            "server": plan["server"],
            "plan_hash": plan["plan_hash"],
            "files_hash": files_hash,
            "expires_at": token.expires_at,
            "action": "workflow_run",
        }

    def approve_submit(self, *, server: str, directory: str, script: str,
                       validity_hours: int = DEFAULT_VALIDITY_HOURS,
                       stdin_lines: list[str] | None = None) -> dict[str, Any]:
        """Mint a one-shot job_submit approval (local interactive flow)."""
        valid_server_name(server)
        valid_filename(script)
        phrase = f"submit {server} {directory} {script}"
        if stdin_lines is not None:
            typed = stdin_lines[0].strip() if stdin_lines else ""
        else:
            import sys
            if not sys.stdin.isatty():
                raise ApprovalError(
                    "submit approval requires an interactive terminal")
            print(f"Submit approval for {server}:{directory} script {script}")
            typed = input(f"Type '{phrase}' to approve: ").strip()
        if typed != phrase:
            raise ApprovalError("confirmation phrase did not match")
        key = self.config.approval_signing_key()
        token = issue_token(
            key, server=server,
            plan_hash=_adhoc_plan_hash(server, directory, script),
            files_hash=text_sha256(f"{server}:{directory}:{script}"),
            action="job_submit",
            parameter_hash=obj_sha256({"directory": directory, "script": script}),
            validity_hours=validity_hours)
        self._record_approval(token, "adhoc")
        if self.audit:
            self.audit.record("workflow.approve_submit", outcome="ok",
                              server=server, token_id=token.token_id)
        return {"approval_ref": token.encode(), "token_id": token.token_id,
                "server": server, "directory": directory, "script": script,
                "expires_at": token.expires_at, "action": "job_submit"}

    def _record_approval(self, token: ApprovalToken, plan_id: str) -> None:
        with exclusive_file_lock(self.config.approvals_path.with_suffix(".lock")):
            data = self.config.load_approvals()
            issued = [row for row in data.get("issued", [])
                      if not (isinstance(row, dict)
                              and row.get("token_id") == token.token_id)]
            issued.append({
                "token_id": token.token_id,
                "plan_id": plan_id,
                "server": token.server,
                "action": token.action,
                "expires_at": token.expires_at,
                "issued_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"),
                # the encoded token is NOT stored: approvals.json never
                # carries reusable references; only the caller keeps it
            })
            data["issued"] = issued
            self.config.save_approvals(data)

    def _ledger_consumed(self) -> dict[str, str]:
        data = self.config.load_approvals()
        consumed = data.get("consumed")
        if not isinstance(consumed, dict):
            return {}
        return {str(k): str(v) for k, v in consumed.items()}

    def _mark_consumed(self, token_id: str, consumer: str) -> None:
        data = self.config.load_approvals()
        consumed = data.get("consumed")
        if not isinstance(consumed, dict):
            consumed = {}
        consumed[token_id] = consumer
        data["consumed"] = consumed
        self.config.save_approvals(data)

    def _consume_approval(self, token_id: str, consumer: str) -> dict[str, str]:
        """Atomically check-and-consume a one-shot approval token."""
        with exclusive_file_lock(self.config.approvals_path.with_suffix(".lock")):
            consumed = self._ledger_consumed()
            if token_id in consumed:
                raise ApprovalError(
                    "approval token was already used by another execution; "
                    "approve again")
            self._mark_consumed(token_id, consumer)
            consumed[token_id] = consumer
            return consumed

    @staticmethod
    def _run_parameters_hash(plan: dict[str, Any]) -> str:
        return obj_sha256({
            "server": plan["server"],
            "remote_dir": plan["remote_dir"],
            "steps": plan["steps"],
            "script": plan["job_script"],
        })

    # ------------------------------------------------------------- run / resume
    def run(self, plan_id: str, approval_ref: str, *,
            poll_seconds: int | None = None,
            download_root: str | Path | None = None,
            force: bool = False, _resume: bool = False) -> dict[str, Any]:
        # Keep the lock for the entire attempt.  This serializes UI retries
        # and, importantly, ensures two processes cannot both pass the
        # approval check before either persists its run instance.
        with exclusive_file_lock(self.run_path(plan_id).with_suffix(".lock")):
            return self._run_locked(
                plan_id, approval_ref, poll_seconds=poll_seconds,
                download_root=download_root, force=force, _resume=_resume)

    def _run_locked(self, plan_id: str, approval_ref: str, *,
                    poll_seconds: int | None,
                    download_root: str | Path | None,
                    force: bool, _resume: bool) -> dict[str, Any]:
        plan = self.load_plan(plan_id)
        files_hash = plan_files_hash(plan)
        key = self.config.approval_signing_key()
        state = self._load_run(plan_id)

        if _resume and state and state.get("status") == "completed":
            return state
        if state and state.get("status") == "running" and not (force or _resume):
            raise ValidationError(
                f"plan {plan_id} has a running attempt; use resume after a "
                "failure, or --force after an interrupted session")

        token = verify_token(
            key, approval_ref,
            server=plan["server"],
            plan_hash=plan["plan_hash"],
            files_hash=files_hash,
            action="workflow_run",
            parameter_hash=self._run_parameters_hash(plan))
        if _resume:
            # only the run instance that consumed the token may resume it
            consumed = self._ledger_consumed()
            if (state is None
                    or state.get("approval_token_id") != token.token_id
                    or consumed.get(token.token_id) != state.get("run_instance")):
                raise ApprovalError(
                    "resume requires the approval bound to this exact run; "
                    "approve again")
            instance = state["run_instance"]
        else:
            instance = f"run-{token.token_id[:8]}-{_now_iso()}"
            self._consume_approval(token.token_id, instance)

        if not state:
            state = {
                "plan_id": plan_id,
                "plan_hash": plan["plan_hash"],
                "files_hash": files_hash,
                "server": plan["server"],
                "created_at": _now_iso(),
                "attempts": [],
            }
        state["run_instance"] = instance
        state["approval_token_id"] = token.token_id
        attempt = {
            "attempt": len(state["attempts"]) + 1,
            "started_at": _now_iso(),
            "steps": [],
            "status": "running",
        }
        state["attempts"].append(attempt)
        state["status"] = "running"
        self._save_run(state)
        return self._execute(plan, state, attempt, approval_ref,
                             poll_seconds=poll_seconds,
                             download_root=download_root)

    def resume(self, plan_id: str, approval_ref: str, *,
               poll_seconds: int | None = None,
               download_root: str | Path | None = None) -> dict[str, Any]:
        state = self._load_run(plan_id)
        if not state:
            raise ValidationError(f"no run state for plan {plan_id}; use run")
        if state.get("status") in {"completed", "needs_review"}:
            return self.status(plan_id)
        plan = self.load_plan(plan_id)
        if plan["plan_hash"] != state.get("plan_hash"):
            raise ApprovalError(
                "the plan changed after this run started; a new approval is "
                "required (prepare + approve again)")
        return self.run(plan_id, approval_ref, poll_seconds=poll_seconds,
                        download_root=download_root, _resume=True)

    def status(self, plan_id: str) -> dict[str, Any]:
        state = self._load_run(plan_id)
        if not state:
            raise ValidationError(f"no run state for plan {plan_id}")
        return state

    def list_plans(self) -> list[dict[str, Any]]:
        """Return compact, persisted plan records for the plan center.

        The plan center is intentionally read-only.  It can discover plans
        created from chat, the web form, or the CLI without requiring a
        second database or an in-memory UI session.
        """
        if not self.plans_dir.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(self.plans_dir.glob("*.json")):
            try:
                plan = json.loads(path.read_text(encoding="utf-8"))
                verify_plan_integrity(plan)
            except (OSError, ValueError, ValidationError):
                # A malformed plan should not make the whole center
                # unavailable; expose it as a repairable record instead.
                rows.append({"plan_id": path.stem, "status": "invalid",
                             "plan_file": str(path)})
                continue
            try:
                state = self._load_run(str(plan["plan_id"]))
            except ValidationError as exc:
                state = {"status": "invalid", "failure": {"step": "state",
                                                               "detail": exc.message}}
            attempts = (state or {}).get("attempts") or []
            last_attempt = attempts[-1] if attempts else {}
            rows.append({
                "plan_id": plan["plan_id"],
                "server": plan.get("server", ""),
                "remote_dir": plan.get("remote_dir", ""),
                "file_count": len(plan.get("files") or []),
                "step_count": len(plan.get("steps") or []),
                "risk_level": (plan.get("risk_summary") or {}).get("level", ""),
                "scheduler": (plan.get("job_script") or {}).get("scheduler", ""),
                "job_name": (plan.get("job_script") or {}).get("job_name", ""),
                "status": (state or {}).get("status", "prepared"),
                "scheduler_state": (state or {}).get("scheduler_state", ""),
                "scientific_converged": (state or {}).get(
                    "scientific_converged"),
                "failure": (state or {}).get("failure"),
                "updated_at": (last_attempt.get("started_at") or
                                (state or {}).get("created_at") or
                                path.stat().st_mtime),
            })
        rows.sort(key=lambda row: str(row.get("updated_at", "")), reverse=True)
        return rows

    # ---------------------------------------------------------- tool gate
    def verify_submit_approval(self, approval_ref: str, *, server: str,
                               directory: str, script: str) -> None:
        """Gate for the model-facing job_submit tool (one-shot)."""
        key = self.config.approval_signing_key()
        token = verify_token(
            key, approval_ref, server=server,
            plan_hash=_adhoc_plan_hash(server, directory, script),
            files_hash=text_sha256(f"{server}:{directory}:{script}"),
            action="job_submit",
            parameter_hash=obj_sha256({"directory": directory, "script": script}))
        try:
            self._consume_approval(token.token_id, f"submit:{directory}:{script}")
        except ApprovalError as exc:
            raise ApprovalError(
                "this submit approval was already used; approve again") from exc

    # ------------------------------------------------------------ internals
    def _load_run(self, plan_id: str) -> dict[str, Any] | None:
        path = self.run_path(plan_id)
        # Status polling runs in a different HTTP thread from the workflow
        # worker. On Windows an open reader can prevent os.replace from
        # publishing the worker's next atomic state snapshot, so serialize
        # the short read/write critical sections with a separate state lock
        # (the run lock intentionally spans the whole execution).
        with exclusive_file_lock(path.with_suffix(".state.lock")):
            if not path.is_file():
                return None
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValidationError(
                    f"run state {path} is unreadable; restore the last valid "
                    "backup before resuming") from exc
        if not isinstance(state, dict):
            raise ValidationError(f"run state {path} is not a JSON object")
        return state

    def _save_run(self, state: dict[str, Any]) -> None:
        target = self.run_path(state["plan_id"])
        with exclusive_file_lock(target.with_suffix(".state.lock")):
            self.config.runs_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.",
                                       suffix=".tmp", dir=str(target.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(state, handle, ensure_ascii=False, indent=2,
                              sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def mark_worker_failed(self, plan_id: str, detail: str) -> dict[str, Any] | None:
        """Persist an unexpected worker exception instead of leaving ``running``."""
        with exclusive_file_lock(self.run_path(plan_id).with_suffix(".lock")):
            state = self._load_run(plan_id)
            if not state or state.get("status") != "running":
                return state
            attempts = state.get("attempts") or []
            if attempts and attempts[-1].get("status") == "running":
                attempts[-1]["status"] = "failed"
                attempts[-1]["finished_at"] = _now_iso()
            state["status"] = "failed"
            state["failure"] = {"step": "worker", "detail": str(detail)}
            self._save_run(state)
            if self.audit:
                self.audit.record("workflow.run", outcome="failed",
                                  plan_id=plan_id, step="worker")
            return state

    def _execute(self, plan: dict, state: dict, attempt: dict,
                 approval_ref: str, *, poll_seconds: int | None,
                 download_root: str | Path | None) -> dict:
        completed_steps = {row["id"] for prev in state["attempts"]
                           if prev is not attempt
                           for row in prev.get("steps", []) if row.get("ok")}
        context: dict[str, Any] = {"approval_ref": approval_ref, "job_id": None}
        for step in plan["steps"]:
            if step["id"] in completed_steps:
                continue
            if step["type"] == "submit" and _uncertain_submit(state):
                detail = (
                    "submit outcome is uncertain because the previous worker "
                    "stopped after submission started; inspect the scheduler "
                    "before retrying to avoid a duplicate VASP job")
                attempt["status"] = "failed"
                state["status"] = "needs_review"
                state["failure"] = {"step": step["id"], "detail": detail}
                self._save_run(state)
                return state
            journal = {"id": step["id"], "type": step["type"],
                       "status": "started", "started_at": _now_iso()}
            attempt["steps"].append(journal)
            # Journal before every external effect.  A crash after sbatch/qsub
            # therefore cannot look like a never-submitted plan on resume.
            self._save_run(state)
            try:
                outcome = self._run_step(plan, state, step, context,
                                         poll_seconds=poll_seconds,
                                         download_root=download_root)
            except rsciError as exc:
                # any operation failure becomes a recorded failed step so a
                # new attempt (resume) can retry it
                outcome = {"ok": False, "type": step["type"],
                           "error": exc.message}
            journal.update(outcome, finished_at=_now_iso(),
                          status="ok" if outcome.get("ok") else "failed")
            self._save_run(state)
            if not outcome.get("ok"):
                attempt["status"] = "failed"
                state["status"] = "failed"
                state["failure"] = {"step": step["id"],
                                    "detail": str(outcome.get("error", ""))}
                self._save_run(state)
                if self.audit:
                    self.audit.record("workflow.run", outcome="failed",
                                      plan_id=plan["plan_id"], step=step["id"])
                return state
        attempt["status"] = "completed"
        progress = self._last_progress(state)
        analysis = self._last_analysis(state)
        scheduler_state = str(progress.get("scheduler_state", "UNKNOWN")) \
            if progress else "UNKNOWN"
        if analysis:
            scientific = bool(analysis.get("scientific_converged"))
        elif progress:
            scientific = bool(progress.get("scientific_converged"))
        else:
            scientific = False
        # The two dimensions never collapse: scheduler-finished but
        # unconverged runs end in needs_review, not completed.
        scheduler_success = scheduler_state == "COMPLETED"
        scheduler_failed = scheduler_state in {
            "FAILED", "TIMEOUT", "CANCELLED", "OUT_OF_MEMORY", "NODE_FAIL",
            "PREEMPTED"}
        state["status"] = (
            "completed" if scheduler_success and scientific else
            "failed" if scheduler_failed else "needs_review")
        state["scheduler_state"] = scheduler_state
        state["scientific_converged"] = scientific
        self._save_run(state)
        if self.audit:
            self.audit.record("workflow.run", outcome="ok",
                              plan_id=plan["plan_id"], scheduler=scheduler_state,
                              scientific_converged=scientific)
        return state

    @staticmethod
    def _last_progress(state: dict) -> dict[str, Any] | None:
        for attempt in reversed(state.get("attempts", [])):
            for row in reversed(attempt.get("steps", [])):
                if row.get("type") == "progress" and row.get("progress"):
                    return row["progress"]
        return None

    @staticmethod
    def _last_analysis(state: dict) -> dict[str, Any] | None:
        for attempt in reversed(state.get("attempts", [])):
            for row in reversed(attempt.get("steps", [])):
                if row.get("type") == "parse" and row.get("analysis"):
                    return row["analysis"]
        return None

    def _run_step(self, plan: dict, state: dict, step: dict, context: dict, *,
                  poll_seconds: int | None,
                  download_root: str | Path | None) -> dict[str, Any]:
        kind = step["type"]
        server = plan["server"]
        if kind == "mkdir":
            result = self.client.mkdir(step["path"], server=server)
            return {"ok": True, "type": kind, "result": result}
        if kind == "upload":
            entry = next((f for f in plan["files"]
                          if f["name"] == step["file"]), None)
            if entry is None:
                return {"ok": False, "type": kind,
                        "error": f"plan file {step['file']} missing"}
            if step["file"] == "run.job.sh":
                content = plan.get("job_script_content") or ""
                if text_sha256(content) != step["sha256"]:
                    return {"ok": False, "type": kind,
                            "error": "rendered job script hash drifted from "
                                     "the approved plan"}
                fd, tmp = tempfile.mkstemp(prefix="rsci-plan-", suffix=".sh")
                with open(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
                try:
                    result = self.client.upload(
                        tmp, step["remote_path"], server=server,
                        expected_sha256=step["sha256"])
                finally:
                    Path(tmp).unlink(missing_ok=True)
            else:
                local = str(entry.get("local_path") or "")
                if not local or not Path(local).is_file():
                    return {"ok": False, "type": kind,
                            "error": f"local source vanished: {local}"}
                current = file_sha256(local)
                if current != step["sha256"]:
                    return {"ok": False, "type": kind,
                            "error": (f"local file {step['file']} changed "
                                      f"since approval ({current[:12]}… != "
                                      f"{step['sha256'][:12]}…)")}
                result = self.client.upload(local, step["remote_path"],
                                            server=server,
                                            expected_sha256=step["sha256"])
            return {"ok": True, "type": kind, "file": step["file"],
                    "sha256": step["sha256"],
                    "status": result.get("status", result.get("ok"))}
        if kind == "validate":
            result = self.client.vasp_validate(step["path"], server=server)
            errors = result.get("errors") or []
            if errors:
                return {"ok": False, "type": kind,
                        "error": "input validation failed: " + "; ".join(
                            str(e) for e in errors)}
            return {"ok": True, "type": kind, "result": result}
        if kind == "submit":
            result = self.client.submit(
                step["path"], step["script"],
                approval_ref=str(context.get("approval_ref") or ""),
                server=server)
            context["job_id"] = result.get("job_id")
            return {"ok": True, "type": kind, "job_id": result.get("job_id")}
        if kind == "monitor":
            job_id = context.get("job_id") or _find_job_id(state)
            if not job_id:
                return {"ok": False, "type": kind,
                        "error": "no job id known for monitoring"}
            monitored = self._monitor_until_terminal(
                str(job_id), server=server,
                timeout_min=int(step.get("timeout_min", 2880)),
                poll_seconds=poll_seconds)
            if not monitored.get("ok"):
                return {"ok": False, "type": kind,
                        "error": str(monitored.get("error", "monitor failed"))}
            context["scheduler_state"] = monitored["state"]
            return {"ok": True, "type": kind, "job_id": job_id,
                    "state": monitored["state"]}
        if kind == "progress":
            result = self.client.vasp_progress(step["path"], server=server)
            scheduler_state = str(context.get("scheduler_state") or "UNKNOWN")
            merged = dict(result)
            merged["scheduler_state"] = scheduler_state
            return {"ok": True, "type": kind, "progress": merged}
        if kind == "download":
            root = Path(download_root) if download_root else \
                self.config.home / "downloads" / plan["plan_id"]
            downloaded = []
            optional = set(step.get("optional_files") or [])
            for name in step.get("files", []):
                valid_filename(name)
                target = root / name
                if target.exists():
                    # Existing output is not trusted: it may belong to a
                    # previous run with the same local download directory.
                    # Download to a temporary sibling and compare hashes.
                    fd, tmp_name = tempfile.mkstemp(
                        prefix=f".verify-{name}-", dir=str(root))
                    os.close(fd)
                    tmp = Path(tmp_name)
                    tmp.unlink(missing_ok=True)
                    try:
                        result = self.client.download(
                            f"{step['path'].rstrip('/')}/{name}", tmp,
                            server=server)
                        remote_sha = str(result.get("sha256") or "")
                        local_sha = file_sha256(target)
                        if remote_sha and remote_sha != local_sha:
                            raise ValidationError(
                                f"local output {name} is stale and will not "
                                "be used; choose a new download directory")
                        downloaded.append({"name": name,
                                           "status": "verified_existing",
                                           "sha256": local_sha})
                    except rsciError as exc:
                        if name in optional and _is_missing_remote(exc):
                            raise ValidationError(
                                f"remote optional output {name} is missing "
                                "but a local file exists; remove or relocate "
                                "the stale local file before retrying") from exc
                        raise
                    finally:
                        tmp.unlink(missing_ok=True)
                    continue
                try:
                    result = self.client.download(
                        f"{step['path'].rstrip('/')}/{name}", target,
                        server=server)
                except rsciError as exc:
                    if name in optional and _is_missing_remote(exc):
                        downloaded.append({"name": name, "status": "missing"})
                        continue
                    raise
                downloaded.append({"name": name, "status": "downloaded",
                                   "sha256": result.get("sha256")})
            return {"ok": True, "type": kind, "directory": str(root),
                    "files": downloaded}
        if kind == "parse":
            root = Path(download_root) if download_root else \
                self.config.home / "downloads" / plan["plan_id"]
            texts = {}
            for name in step.get("sources", []):
                local = root / name
                if local.is_file():
                    texts[name] = local.read_text(encoding="utf-8",
                                                  errors="replace")
            if not texts:
                return {"ok": False, "type": kind,
                        "error": "no downloaded sources to parse"}
            analysis = scientific_status(
                scheduler_state=str(context.get("scheduler_state") or "UNKNOWN"),
                files=texts)
            return {"ok": True, "type": kind, "analysis": analysis}
        return {"ok": False, "type": kind,
                "error": f"unknown step type {kind}"}

    def _monitor_until_terminal(self, job_id: str, *, server: str,
                                timeout_min: int,
                                poll_seconds: int | None) -> dict[str, Any]:
        clock = self._clock or time.monotonic
        deadline = clock() + max(1, timeout_min) * 60
        interval = poll_seconds if poll_seconds is not None \
            else self.monitor_poll_seconds
        polls = 0
        while polls < MAX_MONITOR_POLLS:
            polls += 1
            result = self.client.job_state(job_id, server=server)
            state = str(result.get("state") or "UNKNOWN")
            if state in TERMINAL_SCHEDULER_STATES:
                return {"ok": True, "state": state}
            if clock() >= deadline:
                return {"ok": False,
                        "error": f"monitoring timed out after {timeout_min} min "
                                 f"(job still {state})"}
            if self._clock is None:
                time.sleep(interval)
        return {"ok": False, "error": "monitoring exceeded the poll budget"}


def _find_job_id(state: dict[str, Any]) -> str | None:
    for attempt in reversed(state.get("attempts", [])):
        for row in reversed(attempt.get("steps", [])):
            if row.get("type") == "submit" and row.get("job_id"):
                return str(row["job_id"])
    return None


def _adhoc_plan_hash(server: str, directory: str, script: str) -> str:
    return obj_sha256({"kind": "adhoc-submit", "server": server,
                       "directory": directory, "script": script})


def _validate_plan_id(plan_id: str) -> None:
    if not isinstance(plan_id, str) or len(plan_id) != 16 \
            or not all(ch in "0123456789abcdef" for ch in plan_id):
        raise ValidationError("plan id must be 16 hex characters")


def _uncertain_submit(state: dict[str, Any]) -> bool:
    """Whether a prior attempt entered submit but never journaled its result."""
    return any(
        row.get("type") == "submit" and row.get("status") == "started"
        for attempt in state.get("attempts", [])
        for row in attempt.get("steps", [])
    )


def _is_missing_remote(error: rsciError) -> bool:
    detail = " ".join([error.message, json.dumps(error.detail,
                                                ensure_ascii=False)]).lower()
    return any(marker in detail for marker in (
        "not found", "does not exist", "no such file", "missing"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

"""Workflow engine: preview, prepare, approve, run, resume, status.

Execution model ("immutable plan, one approval, unattended inside the plan"):

  preview    build a deterministic plan; no server contact
  prepare    build + persist the plan under <config>/plans/<plan_id>.json
  approve    LOCAL interactive confirmation mints an HMAC approval bound to
             (server, plan_hash, files_hash); the model never sees the key
  run        verify approval -> execute steps unattended; every step result
             is journaled into the run state; failures append a new attempt
  resume     recover the exact ledger-bound execution; known jobs can be
             observed and collected after the original approval expires
  status     replay the persisted run state

The scheduler-completed and scientific-converged dimensions are recorded
separately everywhere; a scheduler COMPLETED job that did not converge ends
in ``needs_review`` rather than ``completed``.
"""

from __future__ import annotations

import json
import os
import tempfile
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
from ..domains import plan_domain
from .approval import (DEFAULT_VALIDITY_HOURS, ApprovalToken,
                       issue_token, verify_token)
from .plan import (DEFAULT_DOWNLOAD_FILES, build_plan, plan_files_hash,
                   verify_plan_integrity)
from .tasks import SCHEMA, WorkflowTasks, finished, job_id as task_job_id

TERMINAL_SCHEDULER_STATES = {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED",
                             "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED"}
MONITOR_POLL_SECONDS = 30


class WorkflowEngine:
    def __init__(self, *, config: Config, client: GatewayClient,
                 audit: AuditLog | None = None,
                 monitor_poll_seconds: int = MONITOR_POLL_SECONDS,
                 clock: Callable[[], float] | None = None) -> None:
        self.config = config
        self.client = client
        self.audit = audit
        self.monitor_poll_seconds = monitor_poll_seconds
        # Retained for caller compatibility. Durable deadlines use UTC so a
        # process or computer restart cannot reset the monitoring budget.
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
        from .research import build_python_plan, project_store
        project = str(spec.get("project") or "")
        if not project and spec.get("from_dir"):
            source = Path(spec["from_dir"]).resolve()
            if source.parent == self.config.projects_dir.resolve() and source.is_dir():
                project = source.name
        domain = spec.get("domain") or (project_store(self.config).kind(project) if project else "vasp")
        if domain == "general":
            plan = build_python_plan(self.config, {**spec, "project": project})
            return {"plan": plan, "files_hash": plan_files_hash(plan),
                    "next": "review the Python script and risk summary, then approve this exact plan"}
        if domain != "vasp":
            raise ValidationError("unsupported workflow domain")
        if project and project_store(self.config).kind(project) != "vasp":
            raise ValidationError("VASP workflow requires an explicitly enabled VASP project")
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
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValidationError(f"plan {plan_id} is unreadable or damaged; preserved for manual review") from exc
        if not isinstance(plan, dict):
            raise ValidationError(f"plan {plan_id} must be an object; preserved for manual review")
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
                print(f"  execute    : {plan['job_script']['name']} via "
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
    @property
    def tasks(self) -> WorkflowTasks:
        return WorkflowTasks(self)

    def run(self, plan_id: str, approval_ref: str, *,
            poll_seconds: int | None = None,
            download_root: str | Path | None = None,
            force: bool = False, _resume: bool = False) -> dict[str, Any]:
        self.tasks.enqueue(plan_id, approval_ref, poll_seconds=poll_seconds,
                           download_root=download_root, force=force, resume=_resume)
        self.tasks.execute(plan_id)
        return self.status(plan_id)

    def resume(self, plan_id: str, approval_ref: str = "", *,
               poll_seconds: int | None = None,
               download_root: str | Path | None = None,
               extend_monitor: bool = False) -> dict[str, Any]:
        self.tasks.enqueue(plan_id, approval_ref, resume=True,
                           poll_seconds=poll_seconds, download_root=download_root,
                           extend_monitor=extend_monitor)
        self.tasks.execute(plan_id)
        return self.status(plan_id)

    def background(self, plan_id: str, approval_ref: str = "", **options) -> dict:
        from .worker import launch
        self.tasks.enqueue(plan_id, approval_ref, **options)
        return launch(self, plan_id)

    def status(self, plan_id: str) -> dict[str, Any]:
        state = self._load_run(plan_id)
        if not state:
            raise ValidationError(f"no run state for plan {plan_id}")
        task = self._task_view(plan_id, state)
        state.pop("execution_receipt", None)
        try:
            plan = self.load_plan(plan_id)
        except (ValidationError, OSError, ValueError) as exc:
            # A damaged/missing plan must not hide the last persisted job
            # result. This is a read-only view, never permission to resume.
            task.update(phase="blocked", blocked_reason="invalid_plan",
                        last_error="计划缺失或损坏；保留最后已知状态，请先人工核对。")
            state["plan_error"] = str(exc)
        else:
            state.update(domain=plan.get("domain", "vasp"), project=plan.get("project", ""),
                         execution=plan.get("execution", {}))
        state["task"] = task
        return state

    def _task_view(self, plan_id: str, state: dict) -> dict:
        task = dict(state.get("task") or {})
        task["worker_alive"] = self.tasks.alive(plan_id)
        if state.get("schema") not in (None, SCHEMA) or state.get("status") == "invalid":
            task.update(phase="blocked", blocked_reason="invalid_plan",
                        last_error="状态版本未知或记录损坏，保留只读记录，请先人工核对。")
        elif not task.get("phase"):
            if finished(state):
                task["phase"] = "finished"
            elif task["worker_alive"]:
                task["phase"] = "preparing"
            else:
                task.update(phase="blocked", blocked_reason="legacy_confirmation_required",
                            last_error="旧运行缺少可验证的恢复信息，请先核对审批归属和远端作业号。")
        return task

    def list_plans(self) -> list[dict[str, Any]]:
        """Return compact, persisted plan records for the plan center.

        The plan center is intentionally read-only.  It can discover plans
        created from chat, the web form, or the CLI without requiring a
        second database or an in-memory UI session.
        """
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
                "domain": plan.get("domain", "vasp"),
                "project": plan.get("project", ""),
                "execution_state": (state or {}).get("execution_state", ""),
                "scientific_status": (state or {}).get("scientific_status", ""),
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
                "job_id": task_job_id(state or {}),
                "collection_status": (state or {}).get("collection_status"),
                "task": self._task_view(str(plan["plan_id"]), state) if state else {},
                "updated_at": ((state or {}).get("updated_at") or last_attempt.get("started_at") or
                                (state or {}).get("created_at") or
                                path.stat().st_mtime),
            })
        listed = {row["plan_id"] for row in rows}
        for path in sorted(self.config.runs_dir.glob("*.json")):
            if path.name.endswith(".python.json"):
                continue
            if path.stem not in listed:
                rows.append({"plan_id": path.stem, "status": "invalid",
                             "failure": {"step": "plan", "detail": "运行记录缺少对应计划，保留只读记录。"},
                             "task": {"phase": "blocked", "blocked_reason": "invalid_plan"}})
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
        if state.get("plan_id") != plan_id or not isinstance(state.get("attempts"), list):
            raise ValidationError(f"run state {path} has invalid identity or attempts; record is read-only")
        if any(not isinstance(attempt, dict) or not isinstance(attempt.get("steps"), list)
               or any(not isinstance(row, dict) for row in attempt["steps"])
               for attempt in state["attempts"]):
            raise ValidationError(f"run state {path} has invalid step journals; record is read-only")
        if state.get("schema") == "rsci.run/2":
            task = state.get("task")
            if (not isinstance(task, dict) or not isinstance(task.get("download_root"), str)
                    or not isinstance(task.get("poll_seconds"), (int, float))
                    or not 0 <= task["poll_seconds"] <= 3600):
                raise ValidationError(f"run state {path} has invalid task metadata; record is read-only")
        return state

    def _save_run(self, state: dict[str, Any]) -> None:
        state["updated_at"] = _now_iso()
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
            result = plan_domain(plan).validate_remote(self.client, step["path"], server)
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
        if kind == "progress":
            result = plan_domain(plan).progress(self.client, step["path"], server)
            scheduler_state = str(context.get("scheduler_state") or "UNKNOWN")
            merged = dict(result)
            merged["scheduler_state"] = scheduler_state
            return {"ok": True, "type": kind, "progress": merged}
        if kind == "download":
            root = Path(download_root) if download_root else \
                self.config.home / "downloads" / plan["plan_id"]
            downloaded = []
            def record_download(item: dict) -> None:
                downloaded.append(item)
                state["task"]["artifacts"] = list(downloaded)
                self._save_run(state)
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
                        record_download({"name": name,
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
                        record_download({"name": name, "status": "missing"})
                        continue
                    raise
                record_download({"name": name, "status": "downloaded",
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
            analysis = plan_domain(plan).parse(str(context.get("scheduler_state") or "UNKNOWN"), texts)
            return {"ok": True, "type": kind, "analysis": analysis}
        return {"ok": False, "type": kind,
                "error": f"unknown step type {kind}"}


def _adhoc_plan_hash(server: str, directory: str, script: str) -> str:
    return obj_sha256({"kind": "adhoc-submit", "server": server,
                       "directory": directory, "script": script})


def _validate_plan_id(plan_id: str) -> None:
    if not isinstance(plan_id, str) or len(plan_id) != 16 \
            or not all(ch in "0123456789abcdef" for ch in plan_id):
        raise ValidationError("plan id must be 16 hex characters")



def _is_missing_remote(error: rsciError) -> bool:
    detail = " ".join([error.message, json.dumps(error.detail,
                                                ensure_ascii=False)]).lower()
    return any(marker in detail for marker in (
        "not found", "does not exist", "no such file", "missing"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

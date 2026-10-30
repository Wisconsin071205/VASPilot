"""Durable execution admission, recovery and scheduler reconciliation.

The run JSON is the only task record. Its execution receipt is domain-separated
from approval tokens and is useful only for the exact ledger-bound run. No
reusable approval reference is saved or handed to a child process.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import shlex
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from ..core.errors import (ApprovalError, AuthRequiredError, RemoteError,
                           ValidationError, rsciError)
from ..core.hashing import canonical_json, file_sha256
from ..core.locking import exclusive_file_lock, try_file_lock
from ..core.validation import valid_job_id
from ..domains import plan_domain
from .approval import decode_token, verify_token
from .plan import plan_files_hash

if TYPE_CHECKING:
    from .engine import WorkflowEngine

SCHEMA = "rsci.run/2"
TERMINAL = {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "OUT_OF_MEMORY",
            "NODE_FAIL", "PREEMPTED"}
WRITE_STEPS = {"mkdir", "upload", "validate", "submit"}
STICKY_BLOCKS = {"submit_uncertain", "approval_required", "inputs_changed",
                 "server_changed", "invalid_plan", "invalid_receipt",
                 "legacy_confirmation_required", "monitor_timeout", "step_failed",
                 "execution_uncertain", "result_conflict"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp() -> str:
    return now().isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("task timestamp needs a timezone")
    return parsed.astimezone(timezone.utc)


def job_id(state: dict) -> str | None:
    if state.get("job_id"):
        return str(state["job_id"])
    for attempt in reversed(state.get("attempts", [])):
        for row in reversed(attempt.get("steps", [])):
            if row.get("type") == "submit" and row.get("job_id"):
                return str(row["job_id"])
    return None


def uncertain(state: dict) -> bool:
    if job_id(state):
        return False
    # A transport error is just as ambiguous as a killed worker. It must not
    # turn a recorded submit intent into a retryable failed step.
    return any(row.get("type") == "submit"
               for attempt in state.get("attempts", [])
               for row in attempt.get("steps", []))


def finished(state: dict) -> bool:
    return ((state.get("task") or {}).get("phase") == "finished"
            or state.get("status") == "completed"
            or (not state.get("task") and state.get("status") in
                {"failed", "needs_review"} and any(
                    row.get("type") == "parse" and row.get("ok")
                    for a in state.get("attempts", []) for row in a.get("steps", []))))


class Blocked(Exception):
    def __init__(self, reason: str, detail: str):
        self.reason, self.detail = reason, detail
        super().__init__(detail)


class WorkflowTasks:
    def __init__(self, engine: WorkflowEngine):
        self.engine = engine
        self.config, self.client = engine.config, engine.client

    def lock_path(self, plan_id: str) -> Path:
        return self.engine.run_path(plan_id).with_suffix(".lock")

    def alive(self, plan_id: str) -> bool:
        with try_file_lock(self.lock_path(plan_id)) as acquired:
            return not acquired

    def identity(self, plan: dict) -> dict:
        from .local_python import is_local, identity
        if is_local(plan):
            return identity(self.engine, plan)
        entry = self.client.server_entry(plan["server"])
        return {key: getattr(entry, key, "") for key in
                ("target", "port", "remote_root", "scheduler", "route")}

    def sign(self, payload: dict) -> dict:
        encoded = b"rsci.execution/1\n" + canonical_json(payload).encode("utf-8")
        signature = hmac.new(self.config.approval_signing_key(), encoded,
                             "sha256").hexdigest()
        return {"payload": payload, "signature": signature}

    def consumed(self) -> dict[str, str]:
        # Atomic replacement is not enough on Windows: an unlocked reader
        # can keep approvals.json open while another task replaces it.
        with exclusive_file_lock(self.config.approvals_path.with_suffix(".lock")):
            return self.engine._ledger_consumed()

    def receipt(self, plan: dict, state: dict, *, writes: bool = False,
                check_ledger: bool = True) -> dict:
        grant = state.get("execution_receipt") or {}
        payload = grant.get("payload")
        if not isinstance(payload, dict) or not hmac.compare_digest(
                str(grant.get("signature") or ""),
                self.sign(payload)["signature"]):
            raise Blocked("invalid_receipt", "执行凭据无效，不能自动恢复；请核对运行记录。")
        expected = {"plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"],
                    "files_hash": plan_files_hash(plan), "server": plan["server"],
                    "run_instance": state.get("run_instance"),
                    "approval_token_id": state.get("approval_token_id"),
                    "parameter_hash": self.engine._run_parameters_hash(plan),
                    "download_root": state["task"]["download_root"],
                    "poll_seconds": state["task"]["poll_seconds"]}
        if any(payload.get(k) != v for k, v in expected.items()):
            raise Blocked("invalid_receipt", "计划或运行参数与执行凭据不一致。")
        if payload.get("server_identity") != self.identity(plan):
            raise Blocked("server_changed", "服务器目标、路由或根目录发生变化，请先核对配置。")
        if check_ledger and self.consumed().get(
                state["approval_token_id"]) != state["run_instance"]:
            raise Blocked("invalid_receipt", "审批账本不属于本次运行，不能自动恢复。")
        if writes:
            if payload.get("mode") != "execute" or parse_time(payload["expires_at"]) <= now():
                raise Blocked("approval_required", "审批已过期；远端写入或首次提交需要重新审批。")
            for entry in plan["files"]:
                local = entry.get("local_path")
                if local and (not Path(local).is_file()
                              or file_sha256(local) != entry["sha256"]):
                    raise Blocked("inputs_changed", f"local file {entry['name']} changed since approval; 请重新生成计划。")
        return payload

    def _grant(self, plan: dict, state: dict, token, *, mode="execute") -> dict:
        return self.sign({"plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"],
                          "files_hash": plan_files_hash(plan), "server": plan["server"],
                          "run_instance": state["run_instance"],
                          "approval_token_id": token.token_id,
                          "expires_at": token.expires_at, "mode": mode,
                          "parameter_hash": self.engine._run_parameters_hash(plan),
                          "server_identity": self.identity(plan),
                          "download_root": state["task"]["download_root"],
                          "poll_seconds": state["task"]["poll_seconds"]})

    def _activate(self, plan: dict, state: dict) -> None:
        payload = self.receipt(plan, state, check_ledger=False)
        with exclusive_file_lock(self.config.approvals_path.with_suffix(".lock")):
            owner = self.engine._ledger_consumed().get(state["approval_token_id"])
            if owner is None and state["task"]["phase"] == "admitting":
                if parse_time(payload["expires_at"]) <= now():
                    raise Blocked("approval_required", "任务入队时审批已过期，需要重新审批。")
                self.engine._mark_consumed(state["approval_token_id"], state["run_instance"])
            elif owner != state["run_instance"]:
                raise ApprovalError("approval token was already used by another execution")
        if state["task"]["phase"] == "admitting":
            state["task"]["phase"] = "queued"
            self.engine._save_run(state)

    def enqueue(self, plan_id: str, approval_ref: str = "", *, resume=False,
                poll_seconds=None, download_root=None, force=False,
                extend_monitor=False) -> dict:
        with try_file_lock(self.lock_path(plan_id)) as acquired:
            if not acquired:
                raise ValidationError("plan is already executing in another process")
            plan = self.engine.load_plan(plan_id)
            from .local_python import is_local, result_directory
            if is_local(plan):
                destination = result_directory(self.engine, plan)
                if download_root and Path(download_root).expanduser().resolve() != destination.resolve():
                    raise ValidationError("local analysis must collect into its approved project result directory")
                download_root = destination
                if extend_monitor:
                    raise ValidationError("local Python execution has a fixed approved timeout; it cannot be extended after launch")
            state = self.engine._load_run(plan_id)
            if resume and state and finished(state):
                return state
            if state and state.get("schema") not in (None, SCHEMA):
                raise ValidationError("unsupported run state version; record is read-only")
            if state and not (resume or force):
                token = decode_token(approval_ref)
                if (state.get("task", {}).get("phase") in {"admitting", "queued"}
                        and token.token_id == state.get("approval_token_id")):
                    self._activate(plan, state)
                    return state
                raise ApprovalError("approval already used; use resume for this existing run")
            if not state:
                if resume:
                    raise ValidationError(f"no run state for plan {plan_id}; use run")
                # Check replay BEFORE saving admission: a deleted record must
                # not allow an old approval to create another execution.
                token = verify_token(self.config.approval_signing_key(), approval_ref,
                                     server=plan["server"], plan_hash=plan["plan_hash"],
                                     files_hash=plan_files_hash(plan), action="workflow_run",
                                     parameter_hash=self.engine._run_parameters_hash(plan))
                if token.token_id in self.consumed():
                    raise ApprovalError("approval token was already used by another execution")
                interval = self.engine.monitor_poll_seconds if poll_seconds is None else float(poll_seconds)
                if not 0 <= interval <= 3600:
                    raise ValidationError("poll_seconds must be between 0 and 3600")
                state = {"schema": SCHEMA, "plan_id": plan_id,
                         "plan_hash": plan["plan_hash"], "files_hash": plan_files_hash(plan),
                         "server": plan["server"], "created_at": stamp(), "attempts": [],
                         "status": "running", "run_instance": secrets.token_hex(16),
                         "approval_token_id": token.token_id,
                         "task": {"phase": "admitting", "poll_seconds": interval,
                                  "download_root": str(Path(download_root or
                                      self.config.home / "downloads" / plan_id).expanduser().resolve())}}
                state["execution_receipt"] = self._grant(plan, state, token)
                self.engine._save_run(state)
            else:
                if state.get("plan_hash") != plan["plan_hash"]:
                    raise ApprovalError("the plan changed; prepare and approve again")
                if not state.get("task"):
                    self.migrate(plan, state)
                if download_root and str(Path(download_root).expanduser().resolve()) != state["task"]["download_root"]:
                    raise ValidationError("recovery must use the persisted download directory")
                if approval_ref:
                    token = decode_token(approval_ref)
                    if token.token_id != state.get("approval_token_id"):
                        token = verify_token(self.config.approval_signing_key(), approval_ref,
                                             server=plan["server"], plan_hash=plan["plan_hash"],
                                             files_hash=plan_files_hash(plan), action="workflow_run",
                                             parameter_hash=self.engine._run_parameters_hash(plan))
                        if token.token_id in self.consumed():
                            raise ApprovalError("approval token was already used")
                        # Persist the new receipt before consuming its token.
                        state["approval_token_id"] = token.token_id
                        state["task"]["phase"] = "admitting"
                        state["execution_receipt"] = self._grant(plan, state, token)
                        self.engine._save_run(state)
            try:
                self._activate(plan, state)
                self.receipt(plan, state, writes=not job_id(state) and not uncertain(state))
                if uncertain(state):
                    raise Blocked("submit_uncertain", "提交结果待核对；请关联远端作业号，不能自动再次提交。")
                if extend_monitor and job_id(state):
                    minutes = next(s["timeout_min"] for s in plan["steps"] if s["type"] == "monitor")
                    state["task"]["monitor_deadline_at"] = (now() + timedelta(minutes=minutes)).isoformat()
                    self.event(state, "monitor.extended")
            except Blocked as exc:
                self.block(state, exc)
                raise ApprovalError(exc.detail) if exc.reason == "approval_required" else ValidationError(exc.detail)
            state["status"] = "running"
            state["task"].update(phase="queued", blocked_reason=None, last_error=None)
            state.pop("failure", None)
            self.engine._save_run(state)
            return state

    def migrate(self, plan: dict, state: dict) -> None:
        with exclusive_file_lock(self.config.approvals_path.with_suffix(".lock")):
            ledger = self.config.load_approvals()
        issued = next((r for r in ledger.get("issued", [])
                       if r.get("token_id") == state.get("approval_token_id")
                       and r.get("plan_id") == plan["plan_id"]
                       and r.get("server") == plan["server"]
                       and r.get("action") == "workflow_run"), None)
        known = job_id(state)
        if (not known or not issued or state.get("files_hash") != plan_files_hash(plan)
                or self.consumed().get(state.get("approval_token_id")) != state.get("run_instance")):
            raise ValidationError("legacy_confirmation_required: 旧运行无法证明审批归属或作业号，请人工核对；记录未改写。")
        evidence = self.client.job_workdir(known, server=plan["server"])
        if not self._directory_matches(plan, evidence.get("workdir")):
            raise ValidationError("legacy_confirmation_required: 旧作业的工作目录无法核实；记录未改写。")
        roots = [r["directory"] for a in state.get("attempts", []) for r in a.get("steps", [])
                 if r.get("type") == "download" and r.get("directory")]
        state.update(schema=SCHEMA, job_id=known)
        state["task"] = {"phase": "queued", "poll_seconds": 30,
                         "download_root": str(Path(roots[-1] if roots else
                             self.config.home / "downloads" / plan["plan_id"]).resolve())}
        starts = [r.get("started_at") for a in state.get("attempts", []) for r in a.get("steps", [])
                  if r.get("type") == "monitor" and r.get("started_at")]
        if starts:
            minutes = next(s["timeout_min"] for s in plan["steps"] if s["type"] == "monitor")
            state["task"]["monitor_deadline_at"] = (parse_time(starts[0]) + timedelta(minutes=minutes)).isoformat()
        class LegacyToken:
            token_id = state["approval_token_id"]
            expires_at = issued["expires_at"]
        state["execution_receipt"] = self._grant(plan, state, LegacyToken(), mode="observe")
        self.event(state, "legacy.migrated")
        self.engine._save_run(state)

    def event(self, state: dict, event: str, **fields) -> None:
        state.setdefault("events", []).append({"event": event, "at": stamp(), **fields})
        if self.engine.audit:
            self.engine.audit.record("workflow." + event, plan_id=state["plan_id"], **fields)

    def block(self, state: dict, exc: Blocked) -> None:
        state["status"] = ("needs_review" if exc.reason == "submit_uncertain" else
                           "failed" if exc.reason in {"step_failed", "inputs_changed", "invalid_plan"} else "paused")
        state.setdefault("task", {}).update(phase="blocked", blocked_reason=exc.reason,
                                            last_error=exc.detail)
        state["failure"] = {"step": state["task"].get("current_step", "worker"),
                            "detail": exc.detail, "code": exc.reason}
        attempts = state.get("attempts") or []
        if attempts and attempts[-1].get("status") == "running":
            attempts[-1].update(status="failed", finished_at=stamp())
        if state["task"].get("current_step") in {"progress", "download", "parse", "collect"}:
            state["collection_status"] = "blocked"
        self.event(state, "blocked", reason=exc.reason)
        self.engine._save_run(state)

    def execute(self, plan_id: str) -> dict:
        with try_file_lock(self.lock_path(plan_id)) as acquired:
            if not acquired:
                raise ValidationError("plan is already executing in another process")
            state = self.engine._load_run(plan_id)
            if not state:
                raise ValidationError(f"no run state for plan {plan_id}")
            if finished(state):
                return state
            if state.get("schema") not in (None, SCHEMA):
                raise ValidationError("unsupported run state version; record is read-only")
            if not state.get("task"):
                # A legacy record that cannot prove ownership is not rewritten
                # merely because the application scanned it at startup.
                try:
                    self.migrate(self.engine.load_plan(plan_id), state)
                except rsciError as exc:
                    return {**state, "recovery_error": str(exc)}
            try:
                plan = self.engine.load_plan(plan_id)
                self._activate(plan, state)
                self.receipt(plan, state)
                if uncertain(state):
                    raise Blocked("submit_uncertain", "提交结果待核对；请核对并关联远端作业号，禁止自动重提。")
                state["task"]["worker"] = {"pid": os.getpid(), "instance": secrets.token_hex(8),
                                             "started_at": stamp(), "heartbeat_at": stamp()}
                for old in state.get("attempts", []):
                    if old.get("status") == "running":
                        old.update(status="interrupted", finished_at=stamp())
                attempt = {"attempt": len(state["attempts"]) + 1, "started_at": stamp(),
                           "steps": [], "status": "running"}
                state["attempts"].append(attempt)
                state["status"] = "running"
                state["task"].update(blocked_reason=None, last_error=None)
                state.pop("failure", None)
                self.engine._save_run(state)
                return self.execute_steps(plan, state, attempt)
            except Blocked as exc:
                self.block(state, exc)
                return state
            except AuthRequiredError as exc:
                self.block(state, Blocked("auth_required", exc.message))
                return state
            except Exception as exc:
                logging.getLogger(__name__).exception("Task worker failed: %s", plan_id)
                self.block(state, Blocked("invalid_plan" if isinstance(exc, (ValidationError, ApprovalError))
                                         else "worker_error", str(exc)))
                return state
            finally:
                if (state.get("task") or {}).get("worker", {}).get("pid") == os.getpid():
                    state["task"]["worker"].update(exited_at=stamp(), heartbeat_at=stamp())
                    self.engine._save_run(state)

    def execute_steps(self, plan: dict, state: dict, attempt: dict) -> dict:
        from .local_python import is_local, execute
        if is_local(plan):
            return execute(self, plan, state, attempt)
        completed = {r["id"] for a in state["attempts"] for r in a.get("steps", []) if r.get("ok")}
        context = {"job_id": job_id(state), "scheduler_state": state.get("scheduler_state", "UNKNOWN")}
        if context["job_id"]:
            # A known job is never prepared/submitted a second time, including
            # a legacy journal with a missing successful step snapshot.
            monitor = next(s for s in plan["steps"] if s["type"] == "monitor")
            self.step(plan, state, attempt, monitor, context)
            completed.update(s["id"] for s in plan["steps"] if s["type"] in WRITE_STEPS | {"monitor"})
            self.verify_results(state)
        for step in plan["steps"]:
            if step["id"] not in completed:
                self.step(plan, state, attempt, step, context)
        analysis = self.engine._last_analysis(state) or {}
        scheduler = str(state.get("scheduler_state") or "UNKNOWN")
        state.update(plan_domain(plan).conclusion(scheduler, analysis))
        scientific = state.get("scientific_converged")
        state["collection_status"] = "completed"
        attempt.update(status="completed", finished_at=stamp())
        state["task"].update(phase="finished", blocked_reason=None, finished_at=stamp())
        self.event(state, "finished", scheduler_state=scheduler, scientific_converged=scientific)
        self.engine._save_run(state)
        return state

    def step(self, plan: dict, state: dict, attempt: dict, step: dict, context: dict) -> None:
        kind = step["type"]
        self.receipt(plan, state, writes=kind in WRITE_STEPS)
        if kind == "submit" and (job_id(state) or uncertain(state)):
            raise Blocked("submit_uncertain", "提交已经开始，不能再次调用调度器。")
        task = state["task"]
        if kind == "submit":
            # Check resumed upload checkpoints on the remote side too. Do
            # this BEFORE journaling submit intent: this is still read-only.
            task.update(phase="preparing", current_step="verify_submission")
            self.engine._save_run(state)
            retries = 0
            while True:
                self.receipt(plan, state, writes=True)
                try:
                    self.verify_remote_inputs(plan)
                    break
                except Blocked as exc:
                    if exc.reason != "transport_error" or retries >= 8:
                        raise
                    retries += 1
                    self.wait_retry(state, exc, retries)
                    task["phase"] = "preparing"
        phase = ("submitting" if kind == "submit" else "monitoring" if kind == "monitor"
                 else "collecting" if kind in {"progress", "download", "parse"} else "preparing")
        task.update(phase=phase, current_step=step["id"], last_error=None)
        if kind in {"progress", "download", "parse"}:
            state["collection_status"] = "collecting"
        journal = {"id": step["id"], "type": kind, "status": "started", "started_at": stamp()}
        attempt["steps"].append(journal)
        self.engine._save_run(state)
        retries = 0
        while True:
            try:
                self.receipt(plan, state, writes=kind in WRITE_STEPS)
                if kind == "monitor":
                    result = self.monitor(plan, state, context["job_id"], step)
                    context["scheduler_state"] = result["state"]
                    outcome = {"ok": True, "type": kind, "job_id": context["job_id"], **result}
                else:
                    outcome = self.engine._run_step(plan, state, step, context,
                        poll_seconds=task["poll_seconds"], download_root=task["download_root"])
                if kind == "submit":
                    if not outcome.get("job_id"):
                        raise Blocked("submit_uncertain", "调度器没有返回作业号，提交结果待核对。")
                    try:
                        state["job_id"] = valid_job_id(str(outcome["job_id"]))
                    except ValidationError as exc:
                        raise Blocked("submit_uncertain", "调度器返回了无法验证的作业号，请人工核对。") from exc
                    context["job_id"] = state["job_id"]
                if not outcome.get("ok"):
                    raise Blocked("step_failed", str(outcome.get("error") or "执行步骤失败"))
                journal.update(outcome, status="ok", finished_at=stamp())
                self.engine._save_run(state)
                return
            except Blocked:
                raise
            except Exception as exc:
                if kind == "submit":
                    journal.update(status="uncertain", error=str(exc), finished_at=stamp())
                    self.engine._save_run(state)
                    raise Blocked("submit_uncertain", "提交调用后连接中断或返回异常，必须人工核对作业号，禁止自动重提。") from exc
                if isinstance(exc, AuthRequiredError):
                    raise Blocked("auth_required", "服务器登录已失效，请在连接设置中登录后恢复；后台不会弹出登录窗口。") from exc
                if retryable(exc) and retries < 8:
                    retries += 1
                    self.wait_retry(state, exc, retries)
                    task["phase"] = phase
                    continue
                journal.update(status="failed", error=str(exc), finished_at=stamp())
                self.engine._save_run(state)
                raise Blocked("transport_error" if retryable(exc) else "step_failed", str(exc)) from exc

    def wait_retry(self, state: dict, exc: Exception, retries: int) -> None:
        delay = min(60, 5 * (2 ** min(retries - 1, 4)))
        state["task"].update(phase="retrying", last_error=str(exc),
                               next_retry_at=(now() + timedelta(seconds=delay)).isoformat())
        self.engine._save_run(state)
        time.sleep(delay)

    def monitor(self, plan: dict, state: dict, known: str, step: dict) -> dict:
        task = state["task"]
        if not task.get("monitor_deadline_at"):
            starts = [r["started_at"] for a in state.get("attempts", []) for r in a.get("steps", [])
                      if r.get("type") == "submit" and r.get("started_at")]
            origin = parse_time(starts[0]) if starts else now()
            task["monitor_deadline_at"] = (origin + timedelta(minutes=max(1, int(step.get("timeout_min", 2880))))).isoformat()
            self.engine._save_run(state)
        retries = 0
        while True:
            self.receipt(plan, state)
            try:
                report = self.client.job_state(str(known), server=plan["server"])
            except AuthRequiredError as exc:
                raise Blocked("auth_required", "服务器登录已失效，请登录后恢复此任务。") from exc
            except Exception as exc:
                if not retryable(exc) or retries >= 8:
                    raise Blocked("transport_error", str(exc)) from exc
                if now() >= parse_time(task["monitor_deadline_at"]):
                    raise Blocked("monitor_timeout", "监控期限已到；可核对后延长此作业的监控，不会重新提交。") from exc
                retries += 1
                self.wait_retry(state, exc, retries)
                continue
            observed = str(report.get("state") or "UNKNOWN")
            retries = 0
            state["scheduler_state"] = observed
            if observed != "UNKNOWN":
                task["last_known_scheduler_state"] = observed
            task.update(last_checked_at=stamp(), phase="monitoring", next_retry_at=None,
                        last_error="调度器暂无可确认状态，将继续核对。" if observed == "UNKNOWN" else None)
            task.setdefault("worker", {})["heartbeat_at"] = stamp()
            self.engine._save_run(state)
            if observed in TERMINAL:
                return {"state": observed}
            if now() >= parse_time(task["monitor_deadline_at"]):
                raise Blocked("monitor_timeout", f"监控期限已到（远端状态 {observed}）；请核对后延长监控。")
            time.sleep(task["poll_seconds"])

    def verify_results(self, state: dict) -> None:
        """A completed download checkpoint must still describe the local bytes."""
        previous = next((r for a in reversed(state["attempts"]) for r in reversed(a.get("steps", []))
                         if r.get("type") == "download" and r.get("ok")), None)
        if not previous:
            return
        root = Path(state["task"]["download_root"])
        for item in previous.get("files", []):
            target = root / item["name"]
            if item.get("status") == "missing":
                if target.exists():
                    raise Blocked("step_failed", f"原本缺失的结果文件 {item['name']} 出现了本地副本，请先核对。")
            elif (not target.is_file() or target.is_symlink() or not item.get("sha256")
                  or file_sha256(target) != item["sha256"]):
                raise Blocked("step_failed", f"已回收文件 {item['name']} 的哈希发生变化，请核对本地结果。")

    def verify_remote_inputs(self, plan: dict) -> None:
        directory = self.client._resolve(plan["server"], plan["remote_dir"])
        names = " ".join(shlex.quote(entry["name"]) for entry in plan["files"])
        try:
            report = self.client.run_command(
                f"cd -- {shlex.quote(directory)} && sha256sum -- {names}",
                timeout_seconds=60, server=plan["server"])
        except AuthRequiredError:
            raise
        except rsciError as exc:
            raise Blocked("transport_error", "提交前无法核对远端输入哈希；未调用调度器。" + str(exc)) from exc
        found = {}
        for line in str(report.get("stdout") or "").splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                found[parts[1].strip().lstrip("*")] = parts[0]
        if not report.get("ok") or any(found.get(entry["name"]) != entry["sha256"] for entry in plan["files"]):
            raise Blocked("inputs_changed", "远端输入文件或提交脚本与审批哈希不一致；未提交，请重新核对计划。")

    def _directory_matches(self, plan: dict, value) -> bool:
        return bool(value and PurePosixPath(str(value)) == PurePosixPath(
            self.client._resolve(plan["server"], plan["remote_dir"])))

    def reconcile(self, plan_id: str) -> dict:
        plan = self.engine.load_plan(plan_id)
        state = self.engine._load_run(plan_id)
        if not state:
            raise ValidationError(f"no run state for plan {plan_id}")
        if state.get("task"):
            try:
                self.receipt(plan, state)
            except Blocked as exc:
                raise ValidationError(exc.detail) from exc
        from .local_python import is_local, paths, read_result, _owned
        if is_local(plan):
            report = read_result(self.engine, plan, state)
            workspace, _, lock = paths(self.engine, plan)
            return {"ok": True, "plan_id": plan_id, "server": "local", "read_only": True,
                    "execution": "local_python", "directory": str(workspace),
                    "supervisor_alive": _owned(lock), "report": report,
                    "candidates": [], "warnings": ["本地分析不支持关联远端作业号，也不会自动重跑。"]}
        candidates, warnings = [], []
        ids = {job_id(state)} if job_id(state) else set()
        if not ids:
            for read in (self.client.jobs, self.client.recent_jobs):
                try:
                    ids.update(str(row["job_id"]) for row in read(server=plan["server"]).get("jobs", [])[:50]
                               if row.get("job_id"))
                except rsciError as exc:
                    warnings.append(exc.message)
        for known in sorted(ids):
            try:
                evidence = self.client.job_workdir(known, server=plan["server"])
                if self._directory_matches(plan, evidence.get("workdir")):
                    report = self.client.job_state(known, server=plan["server"])
                    candidates.append({"job_id": known, "directory": evidence["workdir"],
                                       "state": report.get("state", "UNKNOWN")})
            except rsciError as exc:
                warnings.append(exc.message)
        return {"ok": True, "plan_id": plan_id, "server": plan["server"],
                "directory": plan["remote_dir"], "candidates": candidates,
                "warnings": warnings, "read_only": True}

    def bind_job(self, plan_id: str, known: str, phrase: str) -> dict:
        from .local_python import is_local
        if is_local(self.engine.load_plan(plan_id)):
            raise ValidationError("本地分析不能关联远端作业号；请使用只读核对。")
        known = valid_job_id(known)
        if phrase != f"bind {plan_id} {known}":
            raise ValidationError(f"请输入确认短语：bind {plan_id} {known}")
        with try_file_lock(self.lock_path(plan_id)) as acquired:
            if not acquired:
                raise ValidationError("plan is already executing")
            plan, state = self.engine.load_plan(plan_id), self.engine._load_run(plan_id)
            if not state or not uncertain(state):
                raise ValidationError("只能为提交结果不明、尚未关联作业的任务关联一次作业号。")
            try:
                self.receipt(plan, state)
            except Blocked as exc:
                raise ValidationError(exc.detail) from exc
            evidence = self.client.job_workdir(known, server=plan["server"])
            report = self.client.job_state(known, server=plan["server"])
            if not self._directory_matches(plan, evidence.get("workdir")) or report.get("state") in (None, "", "UNKNOWN"):
                raise ValidationError("作业工作目录不匹配，或调度器证据不足；未关联。")
            with exclusive_file_lock(self.config.runs_dir / "bindings.lock"):
                for path in self.config.runs_dir.glob("*.json"):
                    try:
                        other = self.engine._load_run(path.stem)
                    except ValidationError:
                        continue
                    if other and other.get("plan_id") != plan_id and other.get("server") == plan["server"] and job_id(other) == known:
                        raise ValidationError("该作业号已经关联到另一个任务。")
                state["job_id"] = known
                state["scheduler_state"] = report["state"]
                state["status"] = "running"
                state["task"].update(phase="queued", blocked_reason=None, last_error=None)
                state.pop("failure", None)
                self.event(state, "submit.bound", job_id=known, directory=evidence["workdir"])
                self.engine._save_run(state)
            return state


def retryable(exc: Exception) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    if not isinstance(exc, (RemoteError, OSError)):
        return False
    low = str(exc).lower()
    return any(s in low for s in ("timeout", "timed out", "connection", "network",
                                  "temporar", "broken pipe", "disconnect", "socket",
                                  "unreachable", "reset", "ssh_failed", "ssh failed", "transport", "超时"))

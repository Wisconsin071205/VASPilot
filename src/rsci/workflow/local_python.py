"""Approval-bound local Python jobs, supervised independently of the UI/monitor.

This is not an OS sandbox. Approved code has the user's OS permissions. The
managed APIs confine snapshots/collection, omit application secrets from the
child environment, and never automatically execute the same logical run twice.
"""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from ..core.config import _atomic_write_json
from ..core.errors import ValidationError
from ..core.hashing import canonical_json, file_sha256
from ..core.locking import exclusive_file_lock, try_file_lock
from ..core.project_paths import MAX_IMPORT_BYTES, atomic_bytes, confined_path, is_link
from ..domains import plan_domain
from .research import project_store
from .tasks import Blocked, parse_time, stamp

_CHILDREN = []


def is_local(plan):
    return (plan.get("execution") or {}).get("kind") == "local_python"


def paths(engine, plan):
    base = engine.run_path(plan["plan_id"])
    return (base.with_suffix(".workspace"), base.with_suffix(".python.json"),
            base.with_suffix(".python.lock"))


def identity(engine, plan):
    store = project_store(engine.config)
    root = store._project_dir(plan["project"])
    if str(root) != plan["project_root"] or store.kind(plan["project"]) != "general":
        raise ValidationError("analysis project identity changed")
    executable = Path(plan["execution"]["python"])
    return {"kind": "local_python", "project_root": str(root),
            "python": str(executable), "python_sha256": file_sha256(executable)}


def result_directory(engine, plan):
    root = project_store(engine.config)._project_dir(plan["project"])
    return confined_path(root, "results/" + plan["plan_id"])


def _signature(engine, payload):
    return hmac.new(engine.config.approval_signing_key(),
                    b"rsci.python-result/1\n" + canonical_json(payload).encode("utf-8"),
                    "sha256").hexdigest()


def _save_result(engine, plan, payload):
    path = paths(engine, plan)[1]
    with exclusive_file_lock(path.with_suffix(".state.lock")):
        _atomic_write_json(path, {"payload": payload, "signature": _signature(engine, payload)})


def read_result(engine, plan, state):
    path = paths(engine, plan)[1]
    try:
        with exclusive_file_lock(path.with_suffix(".state.lock")):
            doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise Blocked("execution_uncertain", "本地执行记录损坏，已保留，禁止自动重跑。") from exc
    payload = doc.get("payload") if isinstance(doc, dict) else None
    if (not isinstance(payload, dict) or not hmac.compare_digest(str(doc.get("signature") or ""),
            _signature(engine, payload)) or payload.get("plan_hash") != plan["plan_hash"]
            or payload.get("run_instance") != state["run_instance"]):
        raise Blocked("execution_uncertain", "本地执行记录与本次审批不匹配，禁止自动重跑。")
    return payload


def _owned(lock_path):
    with try_file_lock(lock_path) as acquired:
        return not acquired


def _snapshot(tasks, plan, state):
    tasks.receipt(plan, state, writes=True)
    workspace = paths(tasks.engine, plan)[0]
    if is_link(workspace):
        raise Blocked("inputs_changed", "分析工作目录不能是符号链接。")
    root = Path(plan["project_root"])
    workspace.mkdir(parents=True, exist_ok=True)
    for entry in plan["files"]:
        source = confined_path(root, entry["name"])
        if str(source) != entry["local_path"] or file_sha256(source) != entry["sha256"]:
            raise Blocked("inputs_changed", "分析输入与审批哈希不一致，请重新生成计划。")
        target = confined_path(workspace, entry["name"])
        data = source.read_bytes()
        import hashlib
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise Blocked("inputs_changed", "复制期间输入发生变化，尚未执行。")
        atomic_bytes(target, data)
    for name in plan["outputs"]:
        target = confined_path(workspace, name)
        if target.exists():
            raise Blocked("result_conflict", "执行前工作目录中已有输出，已停止以避免混入旧结果。")
        target.parent.mkdir(parents=True, exist_ok=True)


def _launch(engine, plan):
    from .worker import command
    argv = command(engine, plan["plan_id"])
    argv[-2] = "_python_job"
    kwargs = {"stdin": subprocess.DEVNULL, "close_fds": True, "cwd": str(engine.config.home.resolve())}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    env = dict(os.environ, RSCI_HOME=str(engine.config.home.resolve()), RSCI_BACKGROUND_WORKER="1")
    log = engine.run_path(plan["plan_id"]).with_suffix(".supervisor.log")
    with log.open("ab") as output:
        child = subprocess.Popen(argv, stdout=output, stderr=output, env=env, **kwargs)
    _CHILDREN[:] = [p for p in _CHILDREN if p.poll() is None]
    _CHILDREN.append(child)


def execute(tasks, plan, state, attempt):
    engine, task = tasks.engine, state["task"]
    if not state.get("local_execution"):
        task.update(phase="preparing", current_step="snapshot")
        engine._save_run(state)
        _snapshot(tasks, plan, state)
        attempt["steps"].append({"id": "snapshot", "type": "snapshot", "ok": True, "finished_at": stamp()})
        # Persist intent BEFORE spawning. Even a launch-time crash is not
        # permission to repeat a script that might have external effects.
        state["local_execution"] = {"requested_at": stamp(), "run_instance": state["run_instance"]}
        state["job_id"] = "local-" + state["run_instance"][:16]
        task.update(phase="executing", current_step="execute")
        tasks.event(state, "python.execution_intent")
        engine._save_run(state)
        try:
            _launch(engine, plan)
        except Exception as exc:
            raise Blocked("execution_uncertain", "本地执行进程未能确认启动；请核对，不会自动重跑。") from exc
    workspace, _, lock = paths(engine, plan)
    task.update(phase="monitoring", current_step="execute")
    while True:
        tasks.receipt(plan, state)
        report = read_result(engine, plan, state)
        if report and report.get("state") in {"COMPLETED", "FAILED", "TIMEOUT"}:
            break
        active = _owned(lock)
        elapsed = time.time() - parse_time(state["local_execution"]["requested_at"]).timestamp()
        if not active and elapsed > 15:
            # A supervisor can finish between the first read and lock check.
            report = read_result(engine, plan, state)
            if report and report.get("state") in {"COMPLETED", "FAILED", "TIMEOUT"}:
                break
            raise Blocked("execution_uncertain", "本地执行进程已退出但没有可信的完成记录；禁止自动重跑，请核对日志和输出。")
        state["execution_state"] = "RUNNING" if active else "STARTING"
        task.update(last_checked_at=stamp(), last_error=None)
        task.setdefault("worker", {})["heartbeat_at"] = stamp()
        engine._save_run(state)
        time.sleep(.25)
    state["execution_state"] = report["state"]
    state["scheduler_state"] = "NOT_APPLICABLE"
    state["exit_code"] = report.get("exit_code")
    state["execution_error"] = report.get("error")
    attempt["steps"].append({"id": "execute", "type": "execute", "ok": report["state"] == "COMPLETED",
                             "exit_code": report.get("exit_code"), "finished_at": report["finished_at"]})
    task.update(phase="collecting", current_step="collect")
    state["collection_status"] = "collecting"
    engine._save_run(state)
    root = result_directory(engine, plan)
    if str(root) != task["download_root"]:
        raise Blocked("invalid_receipt", "分析结果目录与已审批运行不一致。")
    artifacts = []
    for item in report.get("artifacts", []):
        if item["name"] not in plan["outputs"]:
            raise Blocked("result_conflict", "完成记录包含未声明的输出。")
        source = confined_path(workspace, item["name"])
        if not source.is_file() or file_sha256(source) != item["sha256"]:
            raise Blocked("result_conflict", "分析输出在回收前发生变化，已停止回收。")
        relative = item["name"][len("results/"):]
        target = confined_path(root, relative)
        if target.exists() and file_sha256(target) != item["sha256"]:
            raise Blocked("result_conflict", f"已有结果 {relative} 与本次输出不同，不会覆盖。")
        if not target.exists():
            atomic_bytes(target, source.read_bytes())
        if file_sha256(target) != item["sha256"]:
            raise Blocked("result_conflict", "结果复制校验失败。")
        artifacts.append({"name": relative, "project_file": "results/" + plan["plan_id"] + "/" + relative,
                          "sha256": item["sha256"], "size": item["size"]})
        task["artifacts"] = list(artifacts)
        engine._save_run(state)
    state.update(plan_domain(plan).conclusion(report["state"], {}))
    state["missing_outputs"] = sorted(set(plan["outputs"]) - {a["name"] for a in report.get("artifacts", [])})
    state["collection_status"] = "completed" if not state["missing_outputs"] else "incomplete"
    if report["state"] == "COMPLETED" and state["missing_outputs"]:
        state["status"] = "needs_review"
    attempt["steps"].append({"id": "collect", "type": "collect", "ok": not state["missing_outputs"],
                             "directory": str(root), "files": artifacts})
    attempt.update(status=state["status"], finished_at=stamp())
    task.update(phase="finished", current_step="finished", finished_at=stamp(), last_checked_at=stamp())
    tasks.event(state, "finished", execution_state=report["state"], scientific_status="not_assessed")
    engine._save_run(state)
    return state


def _stop_process(child):
    if child.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=15, check=False)
    else:
        os.killpg(child.pid, signal.SIGKILL)
    if child.poll() is None:
        child.kill()
    child.wait(timeout=10)


def main(app, plan_id):
    engine = app.engine()
    plan, state = engine.load_plan(plan_id), engine._load_run(plan_id)
    if not is_local(plan) or not state or not state.get("local_execution"):
        raise ValidationError("local Python job has no admitted execution")
    workspace, result_path, lock_path = paths(engine, plan)
    with try_file_lock(lock_path) as owned:
        if not owned or result_path.exists():
            return 0
        engine.tasks.receipt(plan, state, writes=True)
        if identity(engine, plan)["python_sha256"] != plan["execution"]["python_sha256"]:
            raise ValidationError("Python interpreter changed since approval")
        for item in plan["files"]:
            if file_sha256(confined_path(workspace, item["name"])) != item["sha256"]:
                raise ValidationError("analysis snapshot changed before execution")
        report = {"plan_hash": plan["plan_hash"], "run_instance": state["run_instance"],
                  "state": "RUNNING", "started_at": stamp(), "artifacts": [], "missing_outputs": []}
        _save_result(engine, plan, report)
        child = None
        try:
            execution = plan["execution"]
            argv = [execution["python"], "-I", "-X", "utf8", execution["script"], *execution["args"]]
            env = {k: v for k, v in os.environ.items() if k.upper() in
                   {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"}}
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            log = engine.run_path(plan_id).with_suffix(".python.log")
            with log.open("wb") as output:
                child = subprocess.Popen(argv, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                                         stdout=output, stderr=subprocess.STDOUT, close_fds=True, **kwargs)
                deadline = time.monotonic() + execution["timeout_seconds"]
                while child.poll() is None:
                    if time.monotonic() >= deadline or log.stat().st_size > 8 * 1024 * 1024:
                        _stop_process(child)
                        report.update(state="TIMEOUT", error="Python exceeded its approved time or 8 MiB log limit")
                        break
                    time.sleep(.1)
                report["exit_code"] = child.wait(timeout=10)
            if report["state"] == "RUNNING":
                report["state"] = "COMPLETED" if report["exit_code"] == 0 else "FAILED"
            total = 0
            for name in plan["outputs"]:
                output = confined_path(workspace, name)
                if not output.is_file():
                    report["missing_outputs"].append(name)
                    continue
                size = output.stat().st_size
                total += size
                if size > MAX_IMPORT_BYTES or total > 64 * 1024 * 1024:
                    raise ValidationError("analysis outputs exceed collection limits (16 MiB/file, 64 MiB total)")
                report["artifacts"].append({"name": name, "size": size, "sha256": file_sha256(output)})
        except Exception as exc:
            if child is not None:
                _stop_process(child)
            report.update(state="FAILED", error=str(exc), exit_code=child.returncode if child else None)
        report["finished_at"] = stamp()
        _save_result(engine, plan, report)
    return 0

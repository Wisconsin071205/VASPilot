"""One detached, hidden process per admitted single-stage task.

No HTTP server, system service, autostart entry or token-bearing command line.
The operating-system run lock is the sole ownership mechanism.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ..core.errors import ValidationError
from ..core.locking import try_file_lock
from .tasks import Blocked, SCHEMA, STICKY_BLOCKS, finished

_CHILDREN: list[subprocess.Popen] = []


def command(engine, plan_id: str) -> list[str]:
    engine.run_path(plan_id)  # validate before constructing argv
    prefix = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "rsci"]
    return prefix + ["--config-dir", str(engine.config.home.resolve()),
                     "workflow", "_worker", plan_id]


def launch(engine, plan_id: str) -> dict:
    state = engine._load_run(plan_id)
    if not state:
        raise ValidationError(f"no run state for plan {plan_id}")
    if finished(state):
        return {"ok": True, "started": plan_id, "task_id": plan_id,
                "already_finished": True}
    if engine.tasks.alive(plan_id):
        return {"ok": True, "started": plan_id, "task_id": plan_id,
                "already_running": True}
    # Reap our completed children, but never wait for a live worker.
    _CHILDREN[:] = [child for child in _CHILDREN if child.poll() is None]
    log_path = engine.config.runs_dir / f"{plan_id}.worker.log"
    kwargs = {"stdin": subprocess.DEVNULL, "close_fds": True,
              "cwd": str(engine.config.home.resolve())}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    env = dict(os.environ, RSCI_HOME=str(engine.config.home.resolve()),
               RSCI_BACKGROUND_WORKER="1")
    try:
        with log_path.open("a", encoding="utf-8") as log:
            child = subprocess.Popen(command(engine, plan_id), stdout=log,
                                     stderr=log, env=env, **kwargs)
        _CHILDREN.append(child)
    except OSError as exc:
        with try_file_lock(engine.tasks.lock_path(plan_id)) as acquired:
            if acquired:
                current = engine._load_run(plan_id)
                if current and not finished(current):
                    engine.tasks.block(current, Blocked("launch_failed", str(exc)))
        raise ValidationError("任务已入队，但后台进程启动失败；可恢复，不要重新提交。",
                              detail={"task_id": plan_id}) from exc
    return {"ok": True, "started": plan_id, "task_id": plan_id,
            "accepted": True, "background": True}


def recover(engine) -> dict:
    """Startup recovery is bounded and local except legacy evidence checks.

    Sticky safety blocks require an explicit human action. A live task is
    never migrated or restarted even when its heartbeat appears old.
    """
    result = {"started": [], "active": [], "attention": []}
    if not engine.config.runs_dir.is_dir():
        return result
    for path in sorted(engine.config.runs_dir.glob("*.json")):
        if path.name.endswith(".python.json"):
            continue  # signed supervisor evidence is not a task record
        try:
            state = engine._load_run(path.stem)
            if not state or finished(state):
                continue
            if state.get("schema") not in (None, SCHEMA):
                result["attention"].append({"plan_id": path.stem, "reason": "unsupported_schema"})
                continue
            if engine.tasks.alive(path.stem):
                result["active"].append(path.stem)
                continue
            task = state.get("task") or {}
            if task.get("phase") == "blocked" and task.get("blocked_reason") in STICKY_BLOCKS:
                result["attention"].append({"plan_id": path.stem, "reason": task["blocked_reason"]})
                continue
            # Admission and legacy migration happen inside the worker, under
            # its task lock; startup never blocks on remote SSH queries.
            launch(engine, path.stem)
            result["started"].append(path.stem)
        except Exception as exc:
            result["attention"].append({"plan_id": path.stem, "reason": str(exc)[:300]})
    return result


def main(app, plan_id: str) -> int:
    # stdin is DEVNULL and all SSH calls are noninteractive in a worker.
    try:
        app.engine().tasks.execute(plan_id)
    except ValidationError:
        # Losing a launch race is expected. The owner keeps running.
        if app.engine().tasks.alive(plan_id):
            return 0
        raise
    return 0

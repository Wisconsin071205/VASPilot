"""Detached research workers; JSON checkpoints and OS locks own execution."""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time

from ..core.errors import ValidationError
from ..core.locking import reentrant_file_lock, try_file_lock
from ..core.project_paths import confined_path
from .common import fingerprint, identifier, project, read, resolve_project, save, stamp

ACTIVE = {"queued", "running", "needs_review"}
KINDS = {"parse", "ask", "compare", "glossary", "profile", "report", "install_ocr"}
CHILDREN = []


def path(config, task_id):
    return confined_path(config.home, f"research-tasks/{identifier(task_id)}.json")


def get(config, task_id):
    p = path(config, task_id)
    with reentrant_file_lock(p.with_suffix(".state.lock")):
        row = read(p)
    if row.get("schema") != "rsci.research-task/1" or row.get("task_id") != task_id:
        raise ValidationError("科研任务格式无法识别，已保留记录")
    with try_file_lock(p.with_suffix(".lock")) as acquired:
        row["worker_alive"] = not acquired
    return row


def update(config, task_id, **fields):
    p = path(config, task_id)
    with reentrant_file_lock(p.with_suffix(".state.lock")):
        row = read(p)
        row.update(fields, updated_at=stamp())
        save(p, row)
        return row


def listing(config, name=None):
    pid = project(config, name)[2] if name else None
    result = []
    for p in sorted((config.home / "research-tasks").glob("*.json")):
        try:
            row = get(config, p.stem)
            if pid is None or row.get("project_id") == pid:
                result.append(row)
        except ValidationError as exc:
            result.append({"task_id": p.stem, "status": "needs_review", "error": str(exc), "corrupt": True})
    return result


def enqueue(config, name, kind, params, *, request_id=None, start=True):
    if kind not in KINDS:
        raise ValidationError("unknown research task kind")
    store, _, pid = project(config, name)
    signature = fingerprint({"project_id": pid, "kind": kind, "params": params})
    with reentrant_file_lock(store.index_path.with_suffix(".lock")):
        for row in listing(config, name):
            if row.get("signature") == signature and (row["status"] in ACTIVE or (request_id and row.get("request_id") == request_id)):
                return {"task_id": row["task_id"], "accepted": True, "status": row["status"], "duplicate": True}
        task_id = secrets.token_hex(16)
        row = {"schema": "rsci.research-task/1", "task_id": task_id, "project_id": pid,
               "kind": kind, "params": params, "signature": signature, "request_id": request_id,
               "status": "queued", "phase": "queued", "created_at": stamp(), "updated_at": stamp(),
               "cancel_requested": False, "result": None}
        save(path(config, task_id), row)
    if start:
        launch(config, task_id)
    return {"task_id": task_id, "accepted": True, "status": "queued"}


def launch(config, task_id):
    row = get(config, task_id)
    if row["worker_alive"] or row["status"] in {"completed", "cancelled"}:
        return
    prefix = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "rsci"]
    command = prefix + ["--config-dir", str(config.home.resolve()), "research-task", "_worker", task_id]
    kwargs = dict(stdin=subprocess.DEVNULL, close_fds=True, cwd=str(config.home.resolve()))
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    CHILDREN[:] = [p for p in CHILDREN if p.poll() is None]
    try:
        with path(config, task_id).with_suffix(".log").open("ab") as log:
            CHILDREN.append(subprocess.Popen(command, stdout=log, stderr=log, **kwargs))
    except OSError as exc:
        update(config, task_id, status="needs_review", phase="launch_failed", error=str(exc))
        raise ValidationError("任务已保存，但后台启动失败，请恢复原任务。", detail={"task_id": task_id}) from exc


def cancel(config, task_id):
    row = get(config, task_id)
    if row["status"] in {"completed", "cancelled"}:
        return row
    return update(config, task_id, cancel_requested=True,
                  **({} if row["worker_alive"] else {"status": "cancelled", "phase": "cancelled"}))


def resume(config, task_id):
    with try_file_lock(path(config, task_id).with_suffix(".lock")) as owned:
        row = get(config, task_id)
        if not owned or row["status"] == "completed":
            return row
        update(config, task_id, status="queued", phase="queued", cancel_requested=False, error=None)
    launch(config, task_id)
    return get(config, task_id)


def recover(config):
    for row in listing(config):
        if row.get("corrupt") or row.get("worker_alive") or row["status"] not in {"queued", "running"}:
            continue
        if row["kind"] in {"ask", "compare", "glossary"} and row.get("phase") == "model_request":
            update(config, row["task_id"], status="needs_review", error="模型请求中断，结果未知；请手动重试，可能再次产生费用。")
        else:
            try:
                launch(config, row["task_id"])
            except ValidationError:
                pass


def check_cancel(config, task_id):
    if get(config, task_id).get("cancel_requested"):
        raise InterruptedError("已取消科研任务，已有文件和检查点保留")


def execute(config, task_id):
    with try_file_lock(path(config, task_id).with_suffix(".lock")) as owned:
        if not owned:
            return
        row = get(config, task_id)
        if row["status"] in {"completed", "cancelled"}:
            return
        try:
            check_cancel(config, task_id)
            name = resolve_project(config, row["project_id"])
            update(config, task_id, status="running", pid=os.getpid(), phase="preparing", error=None)
            from . import documents, ocr_runtime
            kind, params = row["kind"], row["params"]
            if kind == "parse":
                result = ocr_runtime.parse(config, name, params["document_id"], task_id)
            elif kind == "install_ocr":
                result = ocr_runtime.install(config, task_id, params.get("offline_bundle"))
            elif kind in {"ask", "compare", "glossary"}:
                from .literature import generate
                result = generate(config, name, kind, params, task_id=task_id)
            elif kind == "profile":
                from .analysis import profile
                result = profile(config, name, **params)
            else:
                from .reports import create
                result = create(config, name, **params)
            check_cancel(config, task_id)
            update(config, task_id, status="completed", phase="completed", result=result)
        except InterruptedError as exc:
            update(config, task_id, status="cancelled", phase="cancelled", error=str(exc))
        except Exception as exc:
            current = get(config, task_id)
            status = "needs_review" if current.get("phase") == "model_request" else "failed"
            update(config, task_id, status=status, error=str(exc)[:1000])

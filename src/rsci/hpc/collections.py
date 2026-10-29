"""Durable result collection, independent of workflow submission and UI life.

Files are checkpointed individually. A lost connection retries collection only;
this module deliberately has no submit/cancel/remote-execution capability.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from ..core.errors import AuthRequiredError, ValidationError
from ..core.hashing import file_sha256, obj_sha256
from ..core.locking import reentrant_file_lock, try_file_lock
from ..core.project_paths import confined_path, relative_name
from ..core.validation import valid_server_name, valid_job_id, remote_path
from ..core.jobhistory import TERMINAL_STATES
from ..workflow.tasks import retryable
from .center import RemoteCenter, project_store, read_record, resolve_project, save_record, stamp

DONE = {"completed", "cancelled"}
_CHILDREN = []


class Collections:
    def __init__(self, config, client, audit=None):
        self.config, self.client, self.audit = config, client, audit
        self.root = config.home / "remote-collections"
        self.center = RemoteCenter(config, client, audit)

    def path(self, task_id):
        if not re.fullmatch(r"[a-f0-9]{32}", task_id or ""):
            raise ValidationError("invalid collection id")
        return self.root / f"{task_id}.json"

    def load(self, task_id):
        state = read_record(self.path(task_id))
        if state.get("schema") != "rsci.collection/1" or state.get("task_id") != task_id:
            raise ValidationError("回收记录版本或身份不匹配，已保留")
        try:
            valid_server_name(state["server"])
            valid_job_id(state["job_id"])
            if (state["identity"]["name"] != state["server"] or
                not isinstance(state.get("project_id"), str) or
                not re.fullmatch(r"[a-f0-9]{32}", state["project_id"]) or
                not isinstance(state.get("events"), list) or
                state["result_relative"] != f"results/remote/{task_id}" or
                state["status"] not in DONE | {"queued", "collecting", "retrying", "blocked"} or
                not isinstance(state["files"], list) or not 1 <= len(state["files"]) <= 100):
                raise ValueError("invalid collection shape")
            remote_path(state["directory"], remote_root=state["identity"]["remote_root"] or "/")
            for item in state["files"]:
                name = relative_name(item["name"])
                if ("/" in name or item["path"] != state["directory"].rstrip("/") + "/" + name or
                    not isinstance(item["size"], int) or item["size"] < 0 or
                    not isinstance(item["mtime_epoch"], int)):
                    raise ValueError("invalid collection file")
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ValidationError("回收记录内容损坏或不安全，已保留原文件") from exc
        return state

    def save(self, state):
        state["updated_at"] = stamp()
        save_record(self.path(state["task_id"]), state)

    def active(self, task_id):
        with try_file_lock(self.path(task_id).with_suffix(".lock")) as acquired:
            return not acquired

    def view(self, state):
        state = dict(state)
        try:
            project = resolve_project(self.config, state["project_id"])
            state["project"] = project["name"]
            state["result_directory"] = str(confined_path(Path(project["path"]), state["result_relative"]))
        except ValidationError:
            state["project_missing"] = True
        return {**state, "worker_active": self.active(state["task_id"]),
                "completed_files": sum(f.get("status") == "completed" for f in state["files"])}

    def list(self):
        tasks, attention = [], []
        for path in sorted(self.root.glob("*.json"), reverse=True):
            try:
                tasks.append(self.view(self.load(path.stem)))
            except ValidationError as exc:
                attention.append({"record": path.name, "error": str(exc)})
        return {"ok": True, "tasks": tasks, "attention": attention}

    @staticmethod
    def snapshot(info):
        if "regular" not in str(info.get("kind", "")).lower():
            raise ValidationError("只支持回收普通文件，目录和符号链接需单独核对")
        return {"size": int(info["size"]), "mtime_epoch": int(info["mtime_epoch"])}

    def enqueue(self, server, job_id, paths):
        identity = self.center.identity(server)
        if not isinstance(paths, list) or not 1 <= len(paths) <= 100 or any(not isinstance(p, str) for p in paths):
            raise ValidationError("请选择 1–100 个结果文件")
        if len(set(paths)) != len(paths):
            raise ValidationError("不能重复选择同一文件")
        details = self.center.details(server, job_id)
        row = details["job"]
        if row.get("state") not in TERMINAL_STATES or row.get("assumed_end"):
            raise ValidationError("需先确认作业已结束再回收；队列消失不代表完成")
        binding = row.get("association")
        if not binding or binding.get("directory") != details["directory"]:
            raise ValidationError("请先核对工作目录并关联项目")
        available = {f["path"] for f in details["files"]}
        if not set(paths).issubset(available):
            raise ValidationError("选中文件不在本次核对的作业目录中")
        files = []
        for path in sorted(paths):
            name = relative_name(PurePosixPath(path).name)
            files.append({"path": path, "name": name,
                          **self.snapshot(self.client.stat(path, server=server))})
        if len({f["name"].casefold() for f in files}) != len(files):
            raise ValidationError("文件名在本地大小写规则下冲突，请分开回收")
        if identity != self.center.identity(server):
            raise ValidationError("回收入队期间服务器配置变化，请重新核对")
        source = {"identity": identity, "job_id": job_id,
                  "directory": details["directory"], "project_id": binding["project_id"], "files": files}
        task_id = obj_sha256(source)[:32]
        store = project_store(self.config)
        with reentrant_file_lock(store.index_path.with_suffix(".lock")):
            project = resolve_project(self.config, binding["project_id"])
            with reentrant_file_lock(self.root / "admission.lock"):
                if self.path(task_id).exists():
                    return self.load(task_id)
                state = {**source, "schema": "rsci.collection/1", "task_id": task_id,
                         "server": server, "scheduler_state": row["state"],
                         "created_at": stamp(), "status": "queued", "error": "",
                         "result_relative": f"results/remote/{task_id}",
                         "result_directory": str(confined_path(Path(project["path"]), f"results/remote/{task_id}")),
                         "events": [{"at": stamp(), "event": "queued"}]}
                self.save(state)
                return state

    def cancel(self, task_id):
        state = self.load(task_id)
        if state["status"] not in DONE:
            # The owner observes this between file operations. No lock stealing.
            self.path(task_id).with_suffix(".cancel").touch()
            with try_file_lock(self.path(task_id).with_suffix(".lock")) as acquired:
                if acquired:
                    state = self.load(task_id)
                    if state["status"] not in DONE:
                        state["status"] = "cancelled"
                        self.save(state)
        return {"ok": True, "task_id": task_id, "message": "停止回收已请求；当前文件传输完成后停止，不取消远端作业。"}

    def resume(self, task_id):
        with try_file_lock(self.path(task_id).with_suffix(".lock")) as acquired:
            if not acquired:
                return launch(self, task_id)
            state = self.load(task_id)
            if state["status"] == "completed":
                return {"ok": True, "task_id": task_id, "already_finished": True}
            if state["identity"] != self.center.identity(state["server"]):
                raise ValidationError("服务器目标已变化，不能恢复原回收任务")
            resolve_project(self.config, state["project_id"])
            self.path(task_id).with_suffix(".cancel").unlink(missing_ok=True)
            state.update(status="queued", error="", retries=0, next_retry_at=None)
            state["events"].append({"at": stamp(), "event": "manual_resume"})
            self.save(state)
        return launch(self, task_id)

    def _collect(self, state):
        if state["identity"] != self.center.identity(state["server"]):
            raise ValidationError("服务器目标已变化，已暂停回收")
        project = resolve_project(self.config, state["project_id"])
        root = Path(project["path"])
        result = confined_path(root, state["result_relative"])
        result.mkdir(parents=True, exist_ok=True)
        state["result_directory"] = str(result)
        # If scheduler metadata is still available it must agree with admission.
        answer = self.client.job_workdir(state["job_id"], server=state["server"])
        if answer.get("workdir") and answer["workdir"].rstrip("/") != state["directory"]:
            raise ValidationError("作业目录发生变化，禁止继续回收")
        for index, item in enumerate(state["files"]):
            if self.path(state["task_id"]).with_suffix(".cancel").exists():
                state["status"] = "cancelled"
                self.save(state)
                return
            if self.center.identity(state["server"]) != state["identity"]:
                raise ValidationError("服务器配置发生变化，已暂停回收")
            target = confined_path(root, state["result_relative"] + "/" + item["name"])
            expected = {key: item[key] for key in ("size", "mtime_epoch")}
            if item.get("status") == "completed":
                if not target.is_file() or file_sha256(target) != item["sha256"]:
                    raise ValidationError(f"已回收文件缺失或被修改：{item['name']}；保留现场，请人工处理")
                continue
            current = self.snapshot(self.client.stat(item["path"], server=state["server"]))
            if current != expected:
                raise ValidationError(f"远端文件已变化：{item['name']}；请重新核对并建立回收记录")
            if target.exists():
                # Covers crash after publish but before final checkpoint. Never
                # adopt an unrelated pre-existing file without our receipt.
                if not item.get("sha256") or file_sha256(target) != item["sha256"]:
                    raise ValidationError(f"本地文件冲突：{item['name']}；不会覆盖")
            else:
                staging = confined_path(root, state["result_relative"] + "/.staging")
                staging.mkdir(exist_ok=True)
                staged = confined_path(root, state["result_relative"] + f"/.staging/{index}.part")
                if staged.exists() and (not item.get("sha256") or file_sha256(staged) != item["sha256"]):
                    staged.unlink()  # our uncommitted scratch, never a user result
                if not staged.exists():
                    if shutil.disk_usage(result).free < item["size"] + 1024 * 1024:
                        raise ValidationError("本地磁盘空间不足，请释放空间后恢复回收")
                    self.client.download(item["path"], staged, server=state["server"])
                after = self.snapshot(self.client.stat(item["path"], server=state["server"]))
                if after != expected:
                    raise ValidationError(f"传输期间远端文件变化：{item['name']}；已暂停")
                item["sha256"] = file_sha256(staged)
                item["status"] = "verified"
                self.save(state)  # durable receipt precedes atomic publication
                try:
                    os.link(staged, target)
                except FileExistsError as exc:
                    raise ValidationError(f"本地文件冲突：{item['name']}") from exc
                except OSError:
                    if os.name != "nt":
                        raise
                    os.rename(staged, target)  # Windows refuses replacement
                staged.unlink(missing_ok=True)
            item["status"] = "completed"
            item["collected_at"] = stamp()
            self.save(state)
        state.update(status="completed", error="", next_retry_at=None)
        state["events"].append({"at": stamp(), "event": "completed"})
        self.save(state)

    def execute(self, task_id):
        with try_file_lock(self.path(task_id).with_suffix(".lock")) as acquired:
            if not acquired:
                return
            state = self.load(task_id)
            if state["status"] in DONE or state["status"] == "blocked":
                return
            state["worker"] = {"pid": os.getpid(), "started_at": stamp()}
            while True:
                try:
                    retry_at = state.get("next_retry_at")
                    if retry_at:
                        seconds = max(0, (datetime.fromisoformat(retry_at) - datetime.now(timezone.utc)).total_seconds())
                        # Short waits also make a requested stop responsive.
                        end = time.monotonic() + min(seconds, 60)
                        while time.monotonic() < end and not self.path(task_id).with_suffix(".cancel").exists():
                            time.sleep(max(0, min(1, end - time.monotonic())))
                    state.update(status="collecting", error="")
                    self.save(state)
                    self._collect(state)
                    return
                except Exception as exc:
                    attempts = state.get("retries", 0) + 1
                    can_retry = retryable(exc) and not isinstance(exc, AuthRequiredError) and attempts <= 5
                    state.update(status="retrying" if can_retry else "blocked",
                                 error=str(exc)[:500], retries=attempts,
                                 next_retry_at=(datetime.now(timezone.utc) + timedelta(seconds=min(60, 5 * 2 ** (attempts - 1)))).isoformat() if can_retry else None)
                    state["events"].append({"at": stamp(), "event": state["status"], "error": state["error"]})
                    self.save(state)
                    if not can_retry:
                        return


def worker_command(config, task_id):
    prefix = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "rsci"]
    return prefix + ["--config-dir", str(config.home.resolve()), "job", "_collect", task_id]


def launch(queue, task_id):
    state = queue.load(task_id)
    if state["status"] in DONE or queue.active(task_id):
        return {"ok": True, "task_id": task_id, "already_running_or_finished": True}
    if state["status"] == "blocked":
        raise ValidationError("原回收任务需要处理；请核对原因后选择继续回收", detail={"task_id": task_id})
    _CHILDREN[:] = [child for child in _CHILDREN if child.poll() is None]
    kwargs = {"stdin": subprocess.DEVNULL, "close_fds": True, "cwd": str(queue.config.home.resolve())}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    env = dict(os.environ, RSCI_HOME=str(queue.config.home.resolve()), RSCI_BACKGROUND_WORKER="1")
    try:
        with queue.path(task_id).with_suffix(".log").open("a", encoding="utf-8") as log:
            child = subprocess.Popen(worker_command(queue.config, task_id), stdout=log, stderr=log, env=env, **kwargs)
            _CHILDREN.append(child)
    except OSError as exc:
        with try_file_lock(queue.path(task_id).with_suffix(".lock")) as acquired:
            if acquired:
                state = queue.load(task_id)
                if state["status"] not in DONE:
                    state.update(status="blocked", error=f"后台启动失败：{exc}")
                    queue.save(state)
        raise ValidationError("回收记录已保存，但后台启动失败；请恢复该记录，不要重新入队",
                              detail={"task_id": task_id}) from exc
    return {"ok": True, "task_id": task_id, "accepted": True, "background": True}


def recover(config, client):
    queue = Collections(config, client)
    result = {"started": [], "attention": []}
    for path in queue.root.glob("*.json"):
        try:
            state = queue.load(path.stem)
            if state["status"] in {"queued", "collecting", "retrying"}:
                launch(queue, path.stem)
                result["started"].append(path.stem)
        except Exception as exc:
            result["attention"].append({"record": path.name, "error": str(exc)[:300]})
    return result

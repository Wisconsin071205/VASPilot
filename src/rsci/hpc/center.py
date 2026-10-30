"""Current-user fleet jobs, identity-bound snapshots and project associations.

This service only reads the cluster. A project association never submits,
resumes, cancels or otherwise takes ownership of a scheduler job.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from ..core.config import _atomic_write_json
from ..core.errors import ValidationError
from ..core.hashing import obj_sha256
from ..core.jobhistory import TERMINAL_STATES
from ..core.locking import reentrant_file_lock
from ..core.validation import remote_path, valid_job_id, valid_server_name
from ..workflow.projects import ProjectStore


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_record(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected object")
        return data
    except (OSError, ValueError) as exc:
        raise ValidationError(f"记录缺失或损坏，已保留：{Path(path).name}") from exc


def save_record(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(Path(path), data)


def project_store(config):
    return ProjectStore(projects_dir=config.projects_dir,
                        index_path=config.projects_index_path)


def resolve_project(config, project_id):
    store = project_store(config)
    with reentrant_file_lock(store.index_path.with_suffix(".lock")):
        for project in store.list():
            if project["project_id"] == project_id:
                return project
    raise ValidationError("原项目已删除或身份发生变化，请处理原关联记录。")


class RemoteCenter:
    def __init__(self, config, client, audit=None):
        self.config, self.client, self.audit = config, client, audit
        self.root = config.home / "remote-center"

    def identity(self, server):
        valid_server_name(server)
        entry = next((s for s in self.config.load_servers() if s.name == server), None)
        if entry is None:
            raise ValidationError("服务器已删除，请检查配置")
        result = {key: getattr(entry, key) for key in
                  ("name", "target", "port", "remote_root", "scheduler", "route")}
        if entry.route == "vlab":
            result["gateway"] = {key: self.config.vlab.get(key) for key in ("host", "port", "user")}
        return result

    def path(self, server):
        return self.root / (obj_sha256(self.identity(server)) + ".json")

    def _load(self, server):
        path = self.path(server)
        if not path.exists():
            data = {"schema": "rsci.remote-center/1", "identity": self.identity(server),
                    "server": server, "jobs": [], "checked_at": None, "error": "尚未查询"}
        else:
            data = read_record(path)
        if (data.get("schema") != "rsci.remote-center/1" or data.get("identity") != self.identity(server)
                or not isinstance(data.get("jobs"), list)
                or any(not isinstance(row, dict) or not isinstance(row.get("job_id"), str)
                       for row in data.get("jobs", []))):
            raise ValidationError("远端作业记录版本不支持，已保留原文件")
        # Short jobs can finish before the first poll. Retain their submission
        # receipts independently of the scheduler's history retention.
        from ..workflow.pending import PendingSubmitStore
        try:
            accepted = PendingSubmitStore(self.config.pending_submits_path).accepted_submissions(data["identity"])
            known = {row["job_id"] for row in data["jobs"]}
            for result in accepted:
                if str(result["job_id"]) not in known:
                    row = self._submitted_row(server, result)
                    data["jobs"].append(row)
                    known.add(row["job_id"])
        except ValidationError as exc:
            data["submission_warning"] = str(exc)
        return data

    def _submitted_row(self, server, result):
        job_id = valid_job_id(str(result.get("job_id") or ""))
        directory = remote_path(str(result.get("directory") or ""),
                                remote_root=self.identity(server)["remote_root"] or "/")
        submitted = str(result.get("submitted_at") or stamp())
        return {"job_id": job_id, "server": server, "directory": directory,
                "name": str(result.get("script") or ""), "script": str(result.get("script") or ""), "state": "UNKNOWN",
                "last_known_state": "SUBMITTED", "source": "submission",
                "submission_id": result.get("submission_id", ""),
                "submitted_at": submitted, "first_seen": submitted, "last_seen": submitted,
                "observed": False, "stale": True}

    def record_submission(self, server, result):
        """Register an accepted receipt without another scheduler request."""
        identity = self.identity(server)
        if result.get("server_identity") != identity:
            raise ValidationError("提交后服务器身份发生变化，已保留提交回执，不能归入当前服务器")
        row = self._submitted_row(server, result)
        path = self.path(server)
        with reentrant_file_lock(path.with_suffix(".lock")):
            data = self._load(server)
            # Repeated receipts must not regress a terminal state.
            if not any(old["job_id"] == row["job_id"] for old in data["jobs"]):
                data["jobs"].append(row)
            save_record(path, data)

    def refresh(self, server):
        identity = self.identity(server)
        path = self.path(server)
        with reentrant_file_lock(path.with_suffix(".lock")):
            data = self._load(server)
            prior = {row["job_id"]: row for row in data["jobs"]}
            checked = stamp()
            try:
                active = self.client.jobs(server=server, timeout=20)
                if active.get("ok") is False:
                    raise ValidationError(str(active.get("error", "查询失败")))
            except Exception as exc:
                data.update(error=str(exc)[:400], attempted_at=checked, stale=True)
                save_record(path, data)
                return data
            warning = ""
            try:
                recent = self.client.recent_jobs(server=server, timeout=20)
                if recent.get("ok") is False:
                    raise ValidationError(str(recent.get("error", "历史查询失败")))
                warning = str(recent.get("warning") or "")
            except Exception as exc:
                recent, warning = {"jobs": []}, str(exc)[:400]
            if identity != self.identity(server):
                raise ValidationError("查询期间服务器配置变化，请重新核对")
            # The live queue takes precedence over lagging accounting records.
            observed = {}
            for row in [*(recent.get("jobs") or []), *(active.get("jobs") or [])]:
                key = str(row.get("job_id") or "")
                if not key or len(key) > 100:
                    continue
                try:
                    valid_job_id(key)
                except ValidationError:
                    row = {**row, "read_only_reason": "数组或特殊作业标识暂仅展示；可在远端终端管理"}
                observed[key] = row
            for key, row in observed.items():
                old = prior.get(key, {})
                directory = str(row.get("directory") or row.get("workdir") or "")
                entry = {**old, **row, "job_id": key, "server": server,
                         "state": str(row.get("state") or "UNKNOWN"),
                         "directory": directory or old.get("directory", ""),
                         "first_seen": old.get("first_seen") or checked,
                         "last_seen": checked, "observed": True, "stale": False}
                if (directory and directory != old.get("directory")) or (
                    row.get("started_at") and old.get("started_at") and row["started_at"] != old["started_at"]):
                    entry.pop("association", None)
                prior[key] = entry
            for key, row in prior.items():
                if key not in observed:
                    row["observed"] = False
                    if row.get("state") not in TERMINAL_STATES:
                        row["last_known_state"] = row.get("last_known_state") or row.get("state")
                        row["state"] = "UNKNOWN"
                        row["stale"] = True
            # Bound unassociated history; project-linked records are retained.
            rows = sorted(prior.values(), key=lambda row: row.get("last_seen", ""), reverse=True)
            retained = [r for i, r in enumerate(rows) if i < 500 or r.get("association")]
            data.update(jobs=retained, checked_at=checked, attempted_at=checked,
                        warning=warning, error="", stale=False)
            save_record(path, data)
            return data

    def overview(self, server="", *, refresh=True):
        names = [server] if server else [entry.name for entry in self.config.load_servers()]
        def one(name):
            try:
                return self.refresh(name) if refresh else self._load(name)
            except Exception as exc:
                return {"server": name, "jobs": [], "stale": True, "error": str(exc)[:400]}
        with ThreadPoolExecutor(max_workers=4) as pool:
            snapshots = list(pool.map(one, names))
        projects = {p["project_id"]: p["name"] for p in project_store(self.config).list()}
        jobs = []
        for snapshot in snapshots:
            for row in snapshot.get("jobs", []):
                binding = row.get("association") or {}
                jobs.append({**row, "server": snapshot["server"],
                             "stale": bool(snapshot.get("stale") or row.get("stale")),
                             "project": projects.get(binding.get("project_id"), ""),
                             "project_missing": bool(binding and binding.get("project_id") not in projects)})
        return {"ok": True, "jobs": jobs,
                "servers": [{k: v for k, v in s.items() if k not in ("jobs", "identity", "schema")}
                            for s in snapshots]}

    def known(self, server, job_id):
        valid_job_id(job_id)
        data = self._load(server)
        row = next((r for r in data["jobs"] if r["job_id"] == job_id), None)
        if row is None:
            raise ValidationError("未在当前用户的作业记录中找到该作业，请先刷新服务器")
        return row

    def directory(self, server, job_id):
        row = self.known(server, job_id)
        recorded = str(row.get("directory") or "")
        answer = self.client.job_workdir(job_id, server=server)
        current = str(answer.get("workdir") or "")
        if current and recorded and current.rstrip("/") != recorded.rstrip("/"):
            raise ValidationError("调度器工作目录与记录不一致，请重新刷新并核对作业")
        directory = current or recorded
        if not directory:
            raise ValidationError("调度器未提供工作目录；证据不足，不能自动关联或回收")
        # Gateway stat additionally rejects symlinks escaping its root.
        remote_path(directory, remote_root=self.identity(server)["remote_root"] or "/")
        info = self.client.stat(directory, server=server)
        if "directory" not in str(info.get("kind") or "").lower():
            raise ValidationError("作业工作目录不存在或无法访问")
        return directory.rstrip("/") or "/"

    def associate(self, server, job_id, project):
        identity = self.identity(server)
        self.refresh(server)
        row = self.known(server, job_id)
        if self._load(server).get("stale"):
            raise ValidationError("服务器当前不可核对，保留原关联，请连接后重试")
        directory = self.directory(server, job_id)
        if identity != self.identity(server):
            raise ValidationError("核对期间服务器配置变化，请重新关联")
        store = project_store(self.config)
        # Same order as project deletion and collection admission.
        with reentrant_file_lock(store.index_path.with_suffix(".lock")):
            meta = store._metadata(project)
            path = self.path(server)
            with reentrant_file_lock(path.with_suffix(".lock")):
                data = self._load(server)
                row = next(r for r in data["jobs"] if r["job_id"] == job_id)
                previous = row.get("association")
                if previous and previous["project_id"] != meta["project_id"]:
                    raise ValidationError("作业已关联其他项目，不能覆盖原关联")
                row.update(directory=directory, association={"project_id": meta["project_id"],
                           "directory": directory, "bound_at": stamp()})
                save_record(path, data)
                if self.audit:
                    self.audit.record("job.associate", server=server, job_id=job_id,
                                      project_id=meta["project_id"], directory=directory)
                return {"ok": True, "job": row, "project": project}

    def details(self, server, job_id):
        row = dict(self.known(server, job_id))
        directory = self.directory(server, job_id)
        warnings = []
        try:
            observed = self.client.job_state(job_id, server=server)
            state = str(observed.get("state") or "UNKNOWN")
            if state != "UNKNOWN":
                row["state"] = state
            else:
                warnings.append("调度器当前无记录，展示最后已知状态；不据此认定完成。")
        except Exception as exc:
            warnings.append(f"状态核对失败：{str(exc)[:200]}")
        found = self.client.find(directory, pattern="*", max_depth=1, limit=201, server=server)
        files = [r for r in found.get("files", [])
                 if PurePosixPath(str(r.get("path", ""))).parent.as_posix() == directory][:200]
        if found.get("truncated") or len(found.get("files", [])) > 200:
            warnings.append("仅展示目录前 200 个文件；其他文件可在远端文件页查看。")
        domain = "vasp" if any(PurePosixPath(r["path"]).name == "INCAR" for r in files) else "general"
        return {"ok": True, "job": row, "server": server, "directory": directory,
                "files": files, "warnings": warnings, "checked_at": stamp(), "domain": domain}

    def text(self, server, job_id, path):
        from .vasp import denied_text_file
        details = self.details(server, job_id)
        if path not in {r["path"] for r in details["files"]}:
            raise ValidationError("文件不在本次核对的作业目录中")
        if denied_text_file(PurePosixPath(path).name):
            raise ValidationError("此文件不支持文本预览，可选择回收")
        return {"ok": True, "path": path, "scope": "末尾最多 240 行",
                "content": str(self.client.tail(path, lines=240, server=server).get("content") or "")[:65536]}

    def diagnose(self, server, job_id):
        from .diagnostics import analyze_job
        details = self.details(server, job_id)
        bundle = self.logs(server, job_id, details=details)
        logs = bundle["logs"]
        details["warnings"].extend(bundle["warnings"])
        progress = {}
        if details["domain"] == "vasp":
            try:
                progress = self.client.vasp_progress(details["directory"], server=server)
            except Exception as exc:
                details["warnings"].append(str(exc)[:160])
        report = analyze_job(job_id=job_id, scheduler_state=details["job"]["state"],
                             progress=progress, logs=logs, domain=details["domain"])
        return {**details, "diagnosis": report, "logs": logs}

    def logs(self, server, job_id, *, directory="", details=None):
        """Shared UI/agent discovery, including finished jobs absent from qstat."""
        from .joblogs import collect_logs
        identity = self.identity(server)
        valid_job_id(job_id)
        if details is None and not directory:
            details = self.details(server, job_id)
        if details is not None:
            row = details["job"]
            directory = details["directory"]
        else:
            directory = remote_path(directory, remote_root=identity["remote_root"] or "/")
            current = self.client.job_workdir(job_id, server=server).get("workdir")
            if current and str(current).rstrip("/") != directory.rstrip("/"):
                raise ValidationError("指定目录与调度器作业目录不一致")
            row = next((r for r in self._load(server)["jobs"] if r["job_id"] == job_id), {})
            if row.get("directory") and row["directory"].rstrip("/") != directory.rstrip("/"):
                raise ValidationError("指定目录与本机作业记录不一致")
        bundle = collect_logs(self.client, server=server, job_id=job_id,
                              directory=directory, script=str(row.get("script") or row.get("name") or ""),
                              scheduler_state=str(row.get("state") or "UNKNOWN"),
                              submitted_at=str(row.get("submitted_at") or ""))
        if identity != self.identity(server):
            raise ValidationError("查找日志期间服务器身份变化，请重新核对")
        bundle["checked_at"] = stamp()
        return bundle

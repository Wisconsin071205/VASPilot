"""Typed client for the named gateway operations.

One method per named operation; parameters pass through
:mod:`vaspilot.core.validation` before reaching the transport. The audit log
records every call with its outcome and hashes. This is the ONLY module the
CLI, the agent tool registry and the MCP server use to touch remote systems.
"""

from __future__ import annotations

from pathlib import Path

from ..core.audit import AuditLog
from ..core.config import Config, ServerEntry
from ..core.errors import RemoteError, ValidationError
from ..core.hashing import file_sha256
from ..core.validation import (confirm_match, remote_path, scheduler_kind,
                               valid_filename, valid_glob, valid_job_id,
                               valid_server_name, valid_trash_id)
from .transport import SshTransport

_TEXT_CAP_BYTES = 2 * 1024 * 1024


class GatewayClient:
    """Facade over the Vlab gateway for all remote operations."""

    def __init__(self, config: Config, transport: SshTransport,
                 audit: AuditLog | None = None) -> None:
        self.config = config
        self.transport = transport
        self.audit = audit

    # ---------------------------------------------------------------- helpers
    def _call(self, event: str, args: list[str], *, timeout: int = 180,
              **audit_fields) -> dict:
        try:
            document = self.transport.run_gateway(args, timeout=timeout)
        except RemoteError as exc:
            if self.audit:
                self.audit.record(event, outcome="failed",
                                  error=exc.to_dict(), **audit_fields)
            raise
        if self.audit:
            self.audit.record(event, outcome="ok", **audit_fields)
        return document

    def _require(self, server: str | None) -> str:
        name = valid_server_name(server or self.config.default_server())
        if not name:
            raise ValidationError("no server given and no default server set")
        return name

    def server_entry(self, server: str) -> ServerEntry:
        for entry in self.config.load_servers():
            if entry.name == server:
                return entry
        raise RemoteError(f"server {server!r} is not in the local mirror; "
                          "run 'vaspilot server list' and reconnect")

    def bound_remote_path(self, server: str, path: str) -> str:
        entry = self.server_entry(server)
        if not entry.remote_root:
            raise ValidationError(
                f"server {server} has no remote_root configured; "
                "set one with 'vaspilot server edit'")
        return remote_path(path, remote_root=entry.remote_root)

    # ------------------------------------------------------------ server ops
    def version(self) -> dict:
        return self._call("gateway.version", ["version"])

    def servers(self) -> dict:
        return self._call("server.list", ["servers"])

    def server_add(self, *, name: str, target: str, port: int = 22,
                   remote_root: str = "", persist: str = "8h",
                   scheduler: str = "auto", set_default: bool = False) -> dict:
        name = valid_server_name(name)
        if scheduler not in ("auto", "slurm", "pbs"):
            raise ValidationError("scheduler must be auto, slurm or pbs")
        result = self._call("server.add", [
            "server-add", name, "--target", target, "--port", str(port),
            "--root", remote_root, "--persist", persist,
            "--scheduler", scheduler])
        # keep the local mirror in sync (metadata only)
        self.config.upsert_server(ServerEntry(
            name=name, target=target, port=port, remote_root=remote_root,
            persist=persist, scheduler=scheduler))
        if set_default:
            self.config.set_default_server(name)
            self.transport.run_gateway(
                ["server-set-default", name])
        return result

    def server_edit(self, name: str, *, target: str | None = None,
                    port: int | None = None, remote_root: str | None = None,
                    persist: str | None = None,
                    scheduler: str | None = None) -> dict:
        name = valid_server_name(name)
        args = ["server-edit", name]
        if target is not None:
            args += ["--target", target]
        if port is not None:
            args += ["--port", str(port)]
        if remote_root is not None:
            args += ["--root", remote_root]
        if persist is not None:
            args += ["--persist", persist]
        if scheduler is not None:
            args += ["--scheduler", scheduler_kind(scheduler)]
        result = self._call("server.edit", args)
        # refresh the mirror entry
        entry = self.server_entry(name)
        if target is not None:
            entry.target = target
        if port is not None:
            entry.port = port
        if remote_root is not None:
            entry.remote_root = remote_root
        if persist is not None:
            entry.persist = persist
        if scheduler is not None:
            entry.scheduler = scheduler
        self.config.upsert_server(entry)
        return result

    def server_remove(self, name: str) -> dict:
        name = valid_server_name(name)
        result = self._call("server.remove", ["server-remove", name])
        self.config.remove_server(name)
        return result

    def server_set_default(self, name: str) -> dict:
        name = valid_server_name(name)
        result = self._call("server.default", ["server-set-default", name])
        self.config.set_default_server(name)
        return result

    def refresh_server_mirror(self) -> list[ServerEntry]:
        """Pull the gateway catalog into the local mirror (metadata only)."""
        catalog = self.servers()
        entries = []
        for item in catalog.get("servers", []):
            if not isinstance(item, dict) or not item.get("name"):
                continue
            entries.append(ServerEntry(
                name=str(item["name"]),
                target=str(item.get("target", "")),
                port=int(item.get("port", 22) or 22),
                remote_root=str(item.get("remote_root", "") or ""),
                persist=str(item.get("persist", "") or ""),
                scheduler=str(item.get("scheduler", "auto") or "auto")))
        self.config.save_servers(entries, default=str(catalog.get("default", "")))
        return entries

    def status(self, server: str | None = None) -> dict:
        name = self._require(server)
        return self._call("server.status", ["status", "--server", name])

    def connect_interactive(self, server: str | None = None) -> dict:
        name = self._require(server)
        result = self.transport.interactive_connect(name)
        if self.audit:
            self.audit.record("server.connect", outcome="ok", server=name)
        return result

    def open_login_terminal(self, server: str | None = None) -> dict:
        """Open the fast-path login terminal for one server.

        The server's target/port/persist come from the local mirror so the
        spawned shell lands on the password prompt with zero gateway hops.
        """
        name = self._require(server)
        entry = self.server_entry(name)
        result = self.transport.open_connect_terminal(
            server=name, target=entry.target, port=entry.port,
            persist=entry.persist or "8h")
        if self.audit:
            self.audit.record("server.login_terminal", outcome="ok", server=name)
        return {"opened": True, "server": name,
                "note": ("enter that server's password and TOTP in the new "
                         "terminal window; existing sessions report alive")}

    def disconnect(self, server: str | None = None) -> dict:
        name = self._require(server)
        return self._call("server.disconnect", ["disconnect", "--server", name])

    def whoami(self, server: str | None = None) -> dict:
        name = self._require(server)
        return self._call("server.whoami", ["whoami", "--server", name])

    # ------------------------------------------------------------- remote fs
    def pwd(self, server: str | None = None) -> dict:
        return self._call("remote.pwd", ["pwd", "--server", self._require(server)])

    def list_dir(self, path: str, *, server: str | None = None) -> dict:
        name = self._require(server)
        return self._call("remote.list",
                          ["list", "--server", name, self._resolve(name, path)],
                          path=path)

    def read(self, path: str, *, server: str | None = None,
             max_bytes: int = 262144) -> dict:
        name = self._require(server)
        result = self._call("remote.read",
                            ["read", "--server", name, self._resolve(name, path)],
                            path=path)
        content = str(result.get("content", ""))
        if len(content.encode("utf-8", "replace")) > _TEXT_CAP_BYTES:
            raise RemoteError("file exceeds the readable text cap")
        return result

    def tail(self, path: str, *, lines: int = 80,
             server: str | None = None) -> dict:
        name = self._require(server)
        lines = max(1, min(int(lines), 2000))
        return self._call("remote.tail",
                          ["tail", "--server", name, self._resolve(name, path),
                           "--lines", str(lines)], path=path)

    def find(self, path: str, *, pattern: str = "*", max_depth: int = 2,
             limit: int = 200, server: str | None = None) -> dict:
        name = self._require(server)
        return self._call("remote.find", [
            "find", "--server", name, self._resolve(name, path),
            "--pattern", valid_glob(pattern),
            "--max-depth", str(max(1, min(max_depth, 8))),
            "--limit", str(max(1, min(limit, 2000)))], path=path)

    def stat(self, path: str, *, server: str | None = None) -> dict:
        name = self._require(server)
        return self._call("remote.stat",
                          ["stat", "--server", name, self._resolve(name, path)],
                          path=path)

    def du(self, path: str, *, server: str | None = None) -> dict:
        name = self._require(server)
        return self._call("remote.du",
                          ["du", "--server", name, self._resolve(name, path)],
                          path=path)

    def mkdir(self, path: str, *, server: str | None = None) -> dict:
        name = self._require(server)
        return self._call("remote.mkdir",
                          ["mkdir", "--server", name, self._resolve(name, path)],
                          path=path)

    def copy(self, path: str, destination: str, *,
             server: str | None = None) -> dict:
        name = self._require(server)
        return self._call("remote.copy", [
            "copy", "--server", name, self._resolve(name, path),
            self._resolve(name, destination)], path=path, to=destination)

    def move(self, path: str, destination: str, *,
             server: str | None = None) -> dict:
        name = self._require(server)
        return self._call("remote.move", [
            "move", "--server", name, self._resolve(name, path),
            self._resolve(name, destination)], path=path, to=destination)

    def remove(self, path: str, *, approval_ref: str = "",
               server: str | None = None) -> dict:
        name = self._require(server)
        args = ["remove", "--server", name, self._resolve(name, path)]
        if approval_ref:
            args += ["--approval-ref", approval_ref[:120]]
        result = self._call("remote.trash", args, path=path)
        if self.audit:
            self.audit.record("remote.trash", outcome="ok", server=name,
                              trash_id=str(result.get("trash_id", "")))
        return result

    def trash_list(self, *, server: str | None = None) -> dict:
        return self._call("remote.trash_list",
                          ["trash-list", "--server", self._require(server)])

    def restore(self, trash_id: str, *, server: str | None = None) -> dict:
        name = self._require(server)
        return self._call("remote.restore",
                          ["restore", "--server", name, valid_trash_id(trash_id)],
                          trash_id=trash_id)

    def purge(self, trash_id: str, confirm_trash_id: str, *,
              server: str | None = None) -> dict:
        name = self._require(server)
        confirm_match(confirm_trash_id, valid_trash_id(trash_id),
                      label="purge")
        return self._call("remote.purge",
                          ["purge", "--server", name, trash_id,
                           "--confirm-trash-id", trash_id],
                          trash_id=trash_id)

    # -------------------------------------------------------- transfer ops
    def upload(self, local_path: str | Path, remote_path_: str, *,
               server: str | None = None, expected_sha256: str | None = None,
               timeout: int = 600) -> dict:
        """Upload one file: scp to a /tmp stage, then gateway-verified move."""
        name = self._require(server)
        target = self._resolve(name, remote_path_)
        local = Path(local_path)
        if not local.is_file() or local.is_symlink():
            raise ValidationError(f"upload source must be a regular file: {local}")
        sha = expected_sha256 or file_sha256(local)
        stage = self.transport.stage_path()
        try:
            self.transport.scp_to_stage(str(local), stage, timeout=timeout)
            result = self._call("remote.upload", [
                "upload", "--server", name, stage, target, sha],
                path=target, sha256=sha, timeout=timeout)
        finally:
            self.transport.rm_stage(stage)
        if str(result.get("sha256", "")) != sha:
            raise RemoteError("upload hash verification failed",
                              detail={"expected": sha,
                                      "got": str(result.get("sha256", ""))})
        return result

    def download(self, path: str, local_path: str | Path, *,
                 server: str | None = None, timeout: int = 600) -> dict:
        name = self._require(server)
        source = self._resolve(name, path)
        local = Path(local_path)
        if local.exists():
            raise ValidationError(
                f"local target {local} already exists; downloads never overwrite")
        local.parent.mkdir(parents=True, exist_ok=True)
        stage = self.transport.stage_path()
        try:
            result = self._call("remote.download", [
                "download", "--server", name, source, stage],
                path=source, timeout=timeout)
            remote_sha = str(result.get("sha256", ""))
            self.transport.scp_from_stage(stage, str(local), timeout=timeout)
        finally:
            self.transport.rm_stage(stage)
        if not local.is_file():
            raise RemoteError("download did not produce a local file")
        local_sha = file_sha256(local)
        if remote_sha and remote_sha != local_sha:
            local.unlink(missing_ok=True)
            raise RemoteError(
                "downloaded file SHA-256 does not match the remote source",
                detail={"remote": remote_sha, "local": local_sha})
        return {"path": source, "local_path": str(local),
                "sha256": local_sha, "size": local.stat().st_size}

    # ------------------------------------------------------------- job ops
    def jobs(self, *, server: str | None = None) -> dict:
        return self._call("job.list", ["jobs", "--server", self._require(server)])

    def recent_jobs(self, *, server: str | None = None) -> dict:
        return self._call("job.recent", ["recent", "--server", self._require(server)])

    def submit(self, directory: str, script: str, *,
               approval_ref: str = "", server: str | None = None) -> dict:
        name = self._require(server)
        args = ["submit", "--server", name, self._resolve(name, directory),
                valid_filename(script)]
        if approval_ref:
            args += ["--approval-ref", approval_ref[:120]]
        result = self._call("job.submit", args, directory=directory, script=script)
        if self.audit:
            self.audit.record("job.submit", outcome="ok", server=name,
                              job_id=str(result.get("job_id", "")),
                              directory=directory)
        return result

    def cancel(self, job_id: str, confirm_job_id: str, *,
               server: str | None = None) -> dict:
        name = self._require(server)
        confirm_match(confirm_job_id, valid_job_id(job_id), label="cancel")
        return self._call("job.cancel",
                          ["cancel", "--server", name, job_id,
                           "--confirm-job-id", job_id], job_id=job_id)

    def job_state(self, job_id: str, *, server: str | None = None) -> dict:
        name = self._require(server)
        return self._call("job.state",
                          ["job-state", "--server", name, valid_job_id(job_id)],
                          job_id=job_id)

    # ------------------------------------------------------------ vasp ops
    def vasp_validate(self, directory: str, *,
                      server: str | None = None) -> dict:
        name = self._require(server)
        return self._call("vasp.validate", [
            "vasp-validate", "--server", name, self._resolve(name, directory)],
            directory=directory)

    def vasp_progress(self, directory: str, *,
                      server: str | None = None) -> dict:
        name = self._require(server)
        return self._call("vasp.progress", [
            "vasp-progress", "--server", name, self._resolve(name, directory)],
            directory=directory)

    def diagnostic(self, key: str, *, server: str | None = None) -> dict:
        name = self._require(server)
        allowed = {"hostname", "system", "python", "disk", "quota",
                   "partitions", "queues", "modules", "scheduler"}
        if key not in allowed:
            raise ValidationError(f"diagnostic must be one of {sorted(allowed)}")
        return self._call("server.diagnostic",
                          ["diagnostic", "--server", name, key], diagnostic=key)

    # ------------------------------------------------------------ shell / metrics
    def run_command(self, command: str, *, timeout_seconds: int = 120,
                    server: str | None = None) -> dict:
        """Arbitrary remote command (operator policy: audit-only, no gate)."""
        name = self._require(server)
        command = str(command or "").strip()
        if not command:
            raise ValidationError("command is required")
        if len(command) > 8000:
            raise ValidationError("command exceeds 8000 chars")
        timeout = max(5, min(int(timeout_seconds or 120), 600)) + 15
        document = self._call("remote.run",
                              ["exec", "--server", name,
                               "--timeout", str(int(timeout_seconds or 120)),
                               "--", command],
                              timeout=timeout)
        rc = document.get("rc")
        return {"ok": rc == 0, "server": name,
                "rc": rc,
                "stdout": str(document.get("stdout", ""))[:20000],
                "stderr": str(document.get("stderr", ""))[:20000],
                "truncated": bool(document.get("truncated")),
                "command": str(document.get("command", command))}

    def metrics(self, server: str | None = None) -> dict:
        """Live node resources parsed from the gateway's tagged sections."""
        name = self._require(server)
        document = self._call("server.metrics",
                              ["metrics", "--server", name], timeout=60)
        sections = document.get("sections") or {}
        parsed = _parse_metric_sections(sections)
        return {"ok": True, "server": name,
                "collected_at": document.get("collected_at"),
                **parsed}

    # ------------------------------------------------------------- internal
    def _resolve(self, server: str, path: str) -> str:
        """Accept an absolute remote path or a root-relative one."""
        entry = self.server_entry(server)
        if not entry.remote_root:
            # no boundary known locally: only absolute paths that the gateway
            # will confine against its own effective root
            if path.startswith("/"):
                return path
            raise ValidationError(
                f"server {server} has no remote_root configured locally")
        if path.startswith("/"):
            return remote_path(path, remote_root=entry.remote_root)
        from ..core.validation import safe_relative_remote
        relative = safe_relative_remote(path)
        parts = relative.split("/")
        return remote_path("/".join([entry.remote_root.rstrip("/")] + parts),
                           remote_root=entry.remote_root)


# ------------------------------------------------------------ metric parsing
def _parse_metric_sections(sections: dict) -> dict:
    """Turn the gateway's raw tagged sections into structured metrics.

    CPU usage comes from the delta between the two /proc/stat samples the
    remote script took; GPU telemetry is clamped (0-100 %, non-negative);
    pseudo mounts are filtered out of the disk table (RackTop approach).
    """
    import re as _re

    def _clamp_pct(value) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(100.0, number))

    def _float(value, default=0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    # -- cpu (two /proc/stat samples; cores from the nproc section)
    cpu: dict = {}
    samples = []
    for key in ("cpu1", "cpu2"):
        line = str(sections.get(key, "")).strip()
        fields = line.split()
        if fields and fields[0] == "cpu" and len(fields) >= 5:
            samples.append([int(f) for f in fields[1:8]
                            if f.lstrip("-").isdigit()][:7])
    cores = int(_float(str(sections.get("nproc", "")).strip(), 0))
    if cores:
        cpu["cores"] = cores
    if len(samples) == 2 and len(samples[0]) == len(samples[1]):
        delta = [b - a for a, b in zip(samples[0], samples[1])]
        total = sum(delta)
        idle = delta[3] + (delta[4] if len(delta) > 4 else 0)
        if total > 0:
            cpu["usage_pct"] = round((total - idle) * 100.0 / total, 1)
    cpu.setdefault("usage_pct", None)

    # -- load
    load = {}
    fields = str(sections.get("load", "")).split()
    if len(fields) >= 3:
        load = {"one": _float(fields[0]), "five": _float(fields[1]),
                "fifteen": _float(fields[2])}

    # -- memory
    mem: dict = {}
    values_kb: dict[str, int] = {}
    for line in str(sections.get("mem", "")).splitlines():
        match = _re.match(r"(\w+):\s+(\d+)\s*kB", line.strip())
        if match:
            values_kb[match.group(1)] = int(match.group(2))
    if values_kb.get("MemTotal"):
        total_kb = values_kb["MemTotal"]
        available = values_kb.get("MemAvailable", 0)
        mem = {"total_gb": round(total_kb / 1048576, 1),
               "available_gb": round(available / 1048576, 1),
               "used_pct": round((total_kb - available) * 100.0 / total_kb, 1)
               if total_kb else 0.0,
               "swap_total_gb": round(values_kb.get("SwapTotal", 0) / 1048576, 1),
               "swap_free_gb": round(values_kb.get("SwapFree", 0) / 1048576, 1)}

    # -- disks (filter pseudo mounts, cap 16)
    pseudo = {"tmpfs", "devtmpfs", "overlay", "squashfs", "fuse.snapfuse",
              "fuse.lxcfs", "nsfs", "proc", "sysfs", "devpts", "cgroup",
              "cgroup2", "mqueue", "shm", "hugetlbfs", "efivarfs",
              "autofs", "binfmt_misc", "configfs", "debugfs", "tracefs",
              "pstore", "securityfs", "fusectl", "rpc_pipefs", "bpf"}
    disks = []
    for line in str(sections.get("df", "")).splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        filesystem, blocks, used, available, capacity, mounted = parts[:6]
        if filesystem.split("/")[-1] in pseudo or filesystem.startswith(
                ("vaspilot/", "/snap/")) or mounted in ("/boot/efi",):
            continue
        disks.append({
            "filesystem": filesystem[:80], "mount": mounted[:80],
            "total_gb": round(_float(blocks) / 1048576, 1),
            "used_gb": round(_float(used) / 1048576, 1),
            "free_gb": round(_float(available) / 1048576, 1),
            "used_pct": _clamp_pct(capacity.rstrip("%")),
        })
        if len(disks) >= 16:
            break

    # -- gpus
    gpus = []
    gpu_raw = str(sections.get("gpu", "")).strip()
    gpu_status = "missing"
    if gpu_raw and gpu_raw not in ("_MISSING_", "_ERR_"):
        gpu_status = "available"
        for line in gpu_raw.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 7:
                continue
            try:
                index = int(parts[0])
            except ValueError:
                continue
            gpus.append({
                "index": index, "name": parts[1][:60],
                "util_pct": _clamp_pct(parts[2]),
                "mem_used_gb": round(_float(parts[3]) / 1024, 1),
                "mem_total_gb": round(_float(parts[4]) / 1024, 1),
                "temp_c": _float(parts[5]),
                "power_w": _float(parts[6]),
            })
    elif gpu_raw == "_ERR_":
        gpu_status = "degraded"

    # -- gpu processes
    gpu_procs = []
    for line in str(sections.get("gpu_proc", "")).splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            gpu_procs.append({
                "gpu_uuid": parts[0][:60],
                "pid": parts[1][:16],
                "process": parts[2][:80],
                "mem_mib": _float(parts[3].split()[0]) if len(parts) > 3 and
                parts[3].split() else None,
            })

    # -- scheduler queue summary
    queue: dict = {}
    sched_lines = [line for line in
                   str(sections.get("sched", "")).splitlines() if line.strip()]
    if sched_lines:
        kind = sched_lines[0].strip()
        queue["kind"] = kind
        partitions = []
        if kind == "slurm":
            for line in sched_lines[1:]:
                parts = line.split("|")
                if len(parts) != 4:
                    continue
                cpus = parts[3]  # A/I/O/T
                match = _re.match(r"(\d+)/(\d+)/(\d+)/(\d+)", cpus)
                partitions.append({
                    "partition": parts[0][:32], "state": parts[1][:16],
                    "nodes": _float(parts[2]),
                    "cpus_alloc": int(match.group(1)) if match else None,
                    "cpus_idle": int(match.group(2)) if match else None,
                    "cpus_total": int(match.group(4)) if match else None,
                })
        elif kind == "pbs":
            for line in sched_lines[1:]:
                parts = line.split()
                if len(parts) >= 6 and not parts[0].startswith("-") \
                        and parts[0] != "Queue":
                    partitions.append({
                        "queue": parts[0][:32], "state": parts[4][:16],
                        "run": _float(parts[5]), "queued": _float(parts[6])
                        if len(parts) > 6 else None,
                        "lm": parts[-2] if len(parts) > 2 else "",
                    })
        queue["partitions"] = partitions

    return {"cpu": cpu, "load": load, "mem": mem, "disks": disks,
            "gpus": gpus, "gpu_status": gpu_status, "gpu_procs": gpu_procs,
            "queue": queue}

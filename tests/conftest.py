"""Shared fixtures: isolated config home, fake gateway transport, VASP inputs."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

ROOT = "/hpc/home/tester/vaspilot-root"


class FakeGatewayState:
    """In-memory model of one gateway + its servers (test double)."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.files: dict[str, dict] = {}     # server -> {posix_path: bytes}
        self.dirs: dict[str, set] = {}       # server -> {posix_path}
        self.trash: dict[str, list] = {}     # server -> [metadata dicts]
        self.connected: dict[str, bool] = {}
        self.servers: dict[str, dict] = {}
        self.default = ""
        self.job_seq = 1000
        self.jobs: dict[str, list] = {}      # server -> [job dicts]
        self.job_states: dict[str, dict] = {}  # server -> {job_id: state}
        self.job_polls: dict[str, int] = {}    # job_id -> poll count
        self.scheduler = {"cl9": "slurm", "pbs1": "pbs"}
        self.key_present: dict[str, bool] = {}
        self.key_verified: dict[str, bool] = {}
        self.key_reject = False
        self.key_hostkey_fail = False
        self.reconnect: dict[str, dict] = {}

    def add_server(self, name, root=ROOT, scheduler="slurm", connected=True):
        self.servers[name] = {"target": f"user@{name}", "port": 22, "root": root,
                              "scheduler": scheduler,
                              "auth_mode": "interactive",
                              "auto_connect": False}
        self.dirs.setdefault(name, {root})
        self.files.setdefault(name, {})
        self.trash.setdefault(name, [])
        self.jobs.setdefault(name, [])
        self.connected[name] = connected
        if not self.default:
            self.default = name


class FakeTransport:
    """Drop-in SshTransport double speaking the gateway JSON protocol."""

    def __init__(self, state: FakeGatewayState):
        self.state = state
        self.calls: list[list] = []

    # -- transport surface ----------------------------------------------------
    def run_gateway(self, args, *, timeout=180, tty=False, capture=True):
        self.calls.append(list(args))
        op = args[0]
        handler = getattr(self, "op_" + op.replace("-", "_"), None)
        if handler is None:
            document = {"ok": True}
        else:
            document = handler(args)
        if isinstance(document, dict) and not document.get("ok") \
                and isinstance(document.get("error"), dict) \
                and document["error"].get("code") == "disconnected":
            from vaspilot.core.errors import AuthRequiredError
            raise AuthRequiredError(document["error"]["message"])
        return document

    def stage_path(self):
        return "/tmp/vaspilot-deadbeef"

    def scp_to_stage(self, local_path, stage, *, timeout=600):
        self._stage = Path(local_path).read_bytes()

    def scp_from_stage(self, stage, local_path, *, timeout=600):
        Path(local_path).write_bytes(self._stage)

    def rm_stage(self, stage):
        self._stage = b""

    def probe_reachable(self, *, timeout=15):
        return True, "fake"

    def interactive_connect(self, server):
        self.state.connected[server] = True
        return {"connected": True, "server": server}

    def open_connect_terminal(self, *, server, target, port=22, persist="8h"):
        self.state.connected[server] = True
        self.state.servers.setdefault(server, {"target": target, "port": port,
                                               "root": "", "scheduler": "auto"})
        return {"opened": True, "server": server}

    # -- helpers ---------------------------------------------------------------
    def _server(self, args):
        if "--server" in args:
            value = args[args.index("--server") + 1]
            if value:
                return value
        return self.state.default

    @staticmethod
    def _positionals(args) -> list:
        """Positional arguments = everything after the --server value."""
        if "--server" in args:
            return list(args[args.index("--server") + 2:])
        return list(args[1:])

    def _in_root(self, server, path):
        root = self.state.servers[server]["root"]
        return path == root or path.startswith(root.rstrip("/") + "/")

    # -- operations --------------------------------------------------------------
    def op_version(self, args):
        return {"ok": True, "gateway_version": "1.3.0", "protocol": "2"}

    def op_servers(self, args):
        state = getattr(self.state, "reconnect", {})
        servers = [{"name": name, "target": e["target"], "port": e["port"],
                    "remote_root": e["root"], "persist": "8h",
                    "scheduler": e["scheduler"],
                    "auth_mode": e.get("auth_mode", "interactive"),
                    "auto_connect": bool(e.get("auto_connect", False)),
                    "reconnect_state": state.get(name, {}).get("state", "-"),
                    "retry_in": state.get(name, {}).get("retry_in", 0),
                    "last_connect_error": state.get(name, {}).get("error", ""),
                    "connected": self.state.connected.get(name, False)}
                   for name, e in self.state.servers.items()]
        return {"ok": True, "servers": servers, "default": self.state.default,
                "gateway_version": "1.3.0", "protocol": "2"}

    def op_server_add(self, args):
        name = args[1]
        self.state.add_server(name, root=args[args.index("--root") + 1],
                              scheduler=args[args.index("--scheduler") + 1])
        return {"ok": True, "added": name}

    def op_server_remove(self, args):
        self.state.servers.pop(args[1], None)
        return {"ok": True, "removed": args[1]}

    def op_server_set_default(self, args):
        self.state.default = args[1]
        return {"ok": True, "default": args[1]}

    def op_server_edit(self, args):
        return {"ok": True, "edited": args[1]}

    def op_status(self, args):
        server = self._server(args)
        if server not in self.state.servers:
            return {"ok": False, "error": {"code": "unknown",
                                           "message": f"unknown server {server}"}}
        if not self.state.connected.get(server):
            return {"ok": False, "error": {
                "code": "disconnected",
                "message": f"{server} has no reusable SSH session; run "
                           f"'vaspilot server connect {server}' in a terminal"}}
        entry = self.state.servers[server]
        return {"ok": True, "server": server, "connected": True,
                "auth_mode": entry.get("auth_mode", "interactive"),
                "auto_connect": bool(entry.get("auto_connect", False))}

    def op_connect(self, args):
        server = self._server(args)
        self.state.connected[server] = True
        entry = self.state.servers[server]
        return {"ok": True, "server": server, "connected": True,
                "auth_mode": entry.get("auth_mode", "interactive"),
                "auto_connect": bool(entry.get("auto_connect", False))}

    def op_disconnect(self, args):
        server = self._server(args)
        self.state.connected[server] = False
        return {"ok": True, "server": server, "connected": False}

    def op_pwd(self, args):
        server = self._server(args)
        if not self.state.connected.get(server):
            return {"ok": False, "error": {
                "code": "disconnected",
                "message": f"{server} has no reusable SSH session; run "
                           f"'vaspilot server connect {server}' in a terminal"}}
        root = self.state.servers[server]["root"]
        return {"ok": True, "server": server, "root": root, "pwd": root}

    def op_list(self, args):
        server = self._server(args)
        positional = [a for a in self._positionals(args) if not a.startswith("--")]
        path = positional[0] if positional else self.state.servers[server]["root"]
        if not self._in_root(server, path):
            return {"ok": False, "error": {"code": "outside_root",
                                           "message": "path outside root"}}
        prefix = path.rstrip("/") + "/"
        explicit = set(self.state.files.get(server, {})) | \
            set(self.state.dirs.get(server, set()))
        children: dict[str, dict] = {}
        for name in explicit:
            name = str(name)
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            if not rel:
                continue
            first, _, rest = rel.partition("/")
            if rest:  # deeper entry -> an implicit directory
                children.setdefault(first, {"type": "dir", "size": 0})
            else:
                is_dir = name in self.state.dirs.get(server, set())
                content = self.state.files[server].get(name, b"")
                children[first] = {"type": "dir" if is_dir else "file",
                                   "size": len(content)}
        entries = [{"name": n, "mtime": "2026-01-01T00:00:00", **meta}
                   for n, meta in sorted(children.items())]
        return {"ok": True, "path": path, "entries": entries}

    def op_read(self, args):
        server = self._server(args)
        path = args[-1]
        if not self._in_root(server, path):
            return {"ok": False, "error": {"code": "outside_root",
                                           "message": "path outside root"}}
        content = self.state.files[server].get(path)
        if content is None:
            return {"ok": False, "error": {"code": "not_found",
                                           "message": f"{path} missing"}}
        return {"ok": True, "path": path, "size": len(content),
                "content": content.decode("utf-8", "replace")}

    def op_tail(self, args):
        document = self.op_read(args)
        if document.get("ok"):
            lines = document["content"].splitlines()
            document["content"] = "\n".join(lines[-80:])
        return document

    def op_find(self, args):
        server = self._server(args)
        path = args[3]
        files = [{"path": p, "size": len(c)}
                 for p, c in self.state.files[server].items()
                 if p.startswith(path.rstrip("/") + "/")]
        return {"ok": True, "root": path, "pattern": "*", "files": files,
                "truncated": False}

    def op_stat(self, args):
        server = self._server(args)
        path = args[-1]
        if path in self.state.files.get(server, {}):
            return {"ok": True, "path": path, "kind": "regular file",
                    "size": len(self.state.files[server][path]),
                    "mtime_epoch": 0, "mtime": "2026-01-01"}
        if path in self.state.dirs.get(server, set()):
            return {"ok": True, "path": path, "kind": "directory", "size": 0,
                    "mtime_epoch": 0, "mtime": "2026-01-01"}
        return {"ok": False, "error": {"code": "not_found",
                                       "message": f"{path} does not exist"}}

    def op_du(self, args):
        server = self._server(args)
        path = args[-1]
        total = sum(len(c) for p, c in self.state.files[server].items()
                    if p.startswith(path.rstrip("/") + "/"))
        return {"ok": True, "path": path, "bytes": total,
                "size_human": f"{total}B"}

    def op_mkdir(self, args):
        server = self._server(args)
        path = args[-1]
        if not self._in_root(server, path):
            return {"ok": False, "error": {"code": "outside_root",
                                           "message": "path outside root"}}
        self.state.dirs[server].add(path)
        return {"ok": True, "path": path, "created": True}

    def op_write(self, args):
        """Staged structured write mirroring the gateway's semantics:
        expected-sha conflict check, then atomic replace."""
        import hashlib
        server = self._server(args)
        sha_idx = args.index("--expected-sha")
        expected = args[sha_idx + 1]
        stage = args[sha_idx + 2]
        path = args[sha_idx + 3]
        if not self._in_root(server, path):
            return {"ok": False, "error": {"code": "outside_root",
                                           "message": "path outside root"}}
        base = str(path).rsplit("/", 1)[-1].upper()
        if base in ("WAVECAR", "CHGCAR", "AECCAR0", "AECCAR2"):
            return {"ok": False, "error": {"code": "text_denylist",
                    "message": f"{base} is not writable as text"}}
        content = getattr(self, "_stage", b"")
        new_sha = hashlib.sha256(content).hexdigest()
        exists = path in self.state.files[server]
        cur_sha = ""
        if exists:
            cur_sha = hashlib.sha256(
                self.state.files[server][path]).hexdigest()
        if (expected or "") != cur_sha:
            return {"ok": False, "error": {"code": "remote_changed",
                    "message": "远端文件已被其他操作修改，请比较后再保存"}}
        self.state.files[server][path] = content
        self.state.dirs[server].add(str(PurePosixPath(path).parent))
        return {"ok": True, "path": path, "sha256": new_sha,
                "size": len(content), "mtime_epoch": 1760000000}

    def op_remove(self, args):
        server = self._server(args)
        positional = [a for a in self._positionals(args)
                      if not a.startswith("--")]
        path = positional[0]
        if not self._in_root(server, path) or path == self.state.servers[server]["root"]:
            return {"ok": False, "error": {"code": "outside_root",
                                           "message": "refused"}}
        import hashlib, time
        # gateway-format trash id: YYYYMMDDTHHMMSSZ-<8 hex>
        trash_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + \
            "-" + hashlib.sha256(path.encode()).hexdigest()[:8]
        meta = {"trash_id": trash_id, "state": "active", "original_path": path,
                "trashed_at": "2026-01-01T00:00:00+00:00", "server": server}
        self.state.trash[server].append(meta)
        for store in (self.state.files[server], ):
            if path in store:
                store[f"{ROOT}/.vaspilot-trash/{trash_id}/payload"] = store.pop(path)
        self.state.dirs[server].discard(path)
        return {"ok": True, "trash_id": trash_id, "moved": path}

    def op_trash_list(self, args):
        server = self._server(args)
        return {"ok": True, "trash": self.state.trash[server]}

    def op_restore(self, args):
        server = self._server(args)
        trash_id = args[-1]
        meta = next((m for m in self.state.trash[server]
                     if m["trash_id"] == trash_id and m["state"] == "active"), None)
        if meta is None:
            return {"ok": False, "error": {"code": "not_found",
                                           "message": "no such trash entry"}}
        payload = f"{ROOT}/.vaspilot-trash/{trash_id}/payload"
        if payload in self.state.files[server]:
            self.state.files[server][meta["original_path"]] = \
                self.state.files[server].pop(payload)
        meta["state"] = "restored"
        return {"ok": True, "restored": meta["original_path"], "trash_id": trash_id}

    def op_purge(self, args):
        server = self._server(args)
        trash_id = self._positionals(args)[0]
        before = len(self.state.trash[server])
        self.state.trash[server] = [m for m in self.state.trash[server]
                                    if m["trash_id"] != trash_id]
        if len(self.state.trash[server]) == before:
            return {"ok": False, "error": {"code": "not_found",
                                           "message": "no such trash entry"}}
        return {"ok": True, "purged": trash_id}

    def op_upload(self, args):
        server = self._server(args)
        stage, path, sha = self._positionals(args)
        import hashlib
        actual = hashlib.sha256(self._stage).hexdigest()
        if actual != sha:
            return {"ok": False, "error": {"code": "sha_mismatch",
                                           "message": "hash mismatch"}}
        if not self._in_root(server, path):
            return {"ok": False, "error": {"code": "outside_root",
                                           "message": "path outside root"}}
        if path in self.state.files[server]:
            return {"ok": False, "error": {"code": "target_exists",
                                           "message": "refusing to overwrite"}}
        self.state.files[server][path] = self._stage
        self.state.dirs[server].add(str(PurePosixPath(path).parent))
        return {"ok": True, "path": path, "sha256": sha, "status": "uploaded"}

    def op_download(self, args):
        server = self._server(args)
        path, stage = self._positionals(args)
        content = self.state.files[server].get(path)
        if content is None:
            return {"ok": False, "error": {"code": "not_found",
                                           "message": f"{path} missing"}}
        import hashlib
        self._stage = content
        return {"ok": True, "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content)}

    def op_jobs(self, args):
        server = self._server(args)
        scheduler = self.state.scheduler.get(server, "slurm")
        jobs = [{"job_id": str(j["job_id"]), "state": j["state"],
                 "elapsed": "0:00", "limit": "0", "partition": "cpu",
                 "name": j.get("name", ""), "nodes": "n1"}
                for j in self.state.jobs[server]
                if j["state"] in ("RUNNING", "PENDING")]
        return {"ok": True, "scheduler": scheduler, "jobs": jobs}

    def op_recent(self, args):
        server = self._server(args)
        return {"ok": True, "scheduler": "slurm",
                "jobs": [{"job_id": str(j["job_id"]), "state": j["state"],
                          "name": j.get("name", ""),
                          "elapsed": j.get("elapsed", ""),
                          "partition": "cpu"}
                         for j in self.state.jobs[server]]}

    def op_submit(self, args):
        server = self._server(args)
        self.state.job_seq += 1
        job_id = str(self.state.job_seq)
        self.state.jobs[server].append({"job_id": job_id, "state": "RUNNING",
                                        "name": "vaspilot"})
        return {"ok": True, "job_id": job_id, "scheduler": "slurm"}

    def op_cancel(self, args):
        job_id = self._positionals(args)[0]
        return {"ok": True, "cancelled": job_id}

    def op_job_state(self, args):
        server = self._server(args)
        job_id = self._positionals(args)[0]
        polls = self.state.job_polls
        polls[job_id] = polls.get(job_id, 0) + 1
        for job in self.state.jobs[server]:
            if str(job["job_id"]) == job_id:
                # simulate the scheduler finishing the job after two polls
                if job["state"] == "RUNNING" and polls[job_id] >= 2:
                    job["state"] = "COMPLETED"
                return {"ok": True, "job_id": job_id, "scheduler": "slurm",
                        "state": job["state"]}
        return {"ok": True, "job_id": job_id, "scheduler": "slurm",
                "state": "COMPLETED"}

    def op_vasp_validate(self, args):
        server = self._server(args)
        directory = args[-1]
        present = [name for name in ("INCAR", "KPOINTS", "POSCAR", "POTCAR",
                                     "run.job.sh")
                   if f"{directory}/{name}" in self.state.files[server]]
        errors = [f"missing required input {n}" for n in
                  ("INCAR", "KPOINTS", "POSCAR") if n not in present]
        return {"ok": True, "directory": directory, "present": sorted(present),
                "errors": errors, "warnings": [], "incar": {}}

    def op_vasp_progress(self, args):
        server = self._server(args)
        directory = args[-1]
        files = self.state.files[server]

        def text(name):
            raw = files.get(f"{directory}/{name}")
            return raw.decode("utf-8", "replace") if raw else ""

        from vaspilot.hpc.vasp import scientific_status
        status = scientific_status(scheduler_state="UNKNOWN", files={
            "INCAR": text("INCAR"), "OSZICAR": text("OSZICAR"),
            "OUTCAR": text("OUTCAR")})
        status.pop("completed", None)
        status["directory"] = directory
        status["files_present"] = [n for n in ("INCAR", "OSZICAR", "OUTCAR")
                                   if f"{directory}/{n}" in files]
        return {"ok": True, **status}

    def op_diagnostic(self, args):
        return {"ok": True, "server": self._server(args),
                "diagnostic": args[-1], "output": "fake diagnostic"}

    def op_exec(self, args):
        server = self._server(args)
        idx = args.index("--") if "--" in args else len(args)
        command = " ".join(args[idx + 1:])
        if not hasattr(self.state, "exec_log"):
            self.state.exec_log = []
        self.state.exec_log.append((server, command))
        if "@@VP_CWD@@" in command:
            # persistent-terminal wrapper: emulate a shell that reports its
            # cwd back; keep any leading "echo x" body for passthrough tests
            import re as _re
            if "boom" in command:
                return {"ok": True, "server": server, "rc": 2,
                        "stdout": "", "stderr": "boom failed\n",
                        "truncated": False, "command": command}
            body = ""
            m = _re.search(r"echo (.+?);\s*__vp_rc", command, _re.S)
            if m:
                body = m.group(1).strip().strip("'\"")
            n = getattr(self.state, "term_n", 0) + 1
            self.state.term_n = n
            out = (body + "\n" if body else "") + f"@@VP_CWD@@/fake/cwd-{n}\n"
            return {"ok": True, "server": server, "rc": 0,
                    "stdout": out, "stderr": "", "truncated": False,
                    "command": command}
        if command.startswith("echo "):
            return {"ok": True, "server": server, "rc": 0,
                    "stdout": command[5:] + "\n", "stderr": "",
                    "truncated": False, "command": command}
        if ".vp-monitor/collector.sh" in command:
            # daemon status probe; flip state.monitor_installed to simulate
            if getattr(self.state, "monitor_installed", False):
                return {"ok": True, "server": server, "rc": 0,
                        "stdout": "INSTALLED\nUP\n", "stderr": "",
                        "truncated": False, "command": command}
            return {"ok": True, "server": server, "rc": 0,
                    "stdout": "ABSENT\n", "stderr": "", "truncated": False,
                    "command": command}
        if "pkill" in command and ".vp-monitor/daemon.sh" in command:
            return {"ok": True, "server": server, "rc": 0,
                    "stdout": "GONE\n", "stderr": "", "truncated": False,
                    "command": command}
        if ".vp-monitor" in command and "nohup" in command:
            return {"ok": True, "server": server, "rc": 0,
                    "stdout": "VP_UP\n", "stderr": "", "truncated": False,
                    "command": command}
        if "hist.tsv" in command or "__VP_USE__" in command:
            tails = getattr(self.state, "monitor_tails", "")
            return {"ok": True, "server": server, "rc": 0,
                    "stdout": tails, "stderr": "", "truncated": False,
                    "command": command}
        if "boom" in command:
            return {"ok": True, "server": server, "rc": 2, "stdout": "",
                    "stderr": "boom failed\n", "truncated": False,
                    "command": command}
        return {"ok": True, "server": server, "rc": 0, "stdout": "",
                "stderr": "", "truncated": False, "command": command}

    def op_metrics(self, args):
        server = self._server(args)
        return {"ok": True, "server": server,
                "collected_at": "2026-01-01T00:00:00+00:00",
                "sections": {
                    "cpu1": "cpu  100 20 30 1000 10 0 0 0",
                    "cpu2": "cpu  200 40 60 1100 10 0 0 0",
                    "load": "0.52 0.41 0.35 1/512 12345",
                    "nproc": "32",
                    "mem": "MemTotal:       16384000 kB\n"
                           "MemAvailable:    8192000 kB\n"
                           "SwapTotal:       2097152 kB\n"
                           "SwapFree:        2097152 kB",
                    "df": "Filesystem     1024-blocks      Used Available "
                          "Capacity Mounted on\n"
                          "tmpfs            8192000        0   8192000       "
                          "0% /dev/shm\n"
                          "/dev/sda1      500000000 200000000 300000000     "
                          "40% /",
                    "gpu": "0, GPU-fake-a100-0, NVIDIA A100, 5, 1200, 32510, 45, 70.5",
                    "gpu_proc": "GPU-fake-a100-0, 12345, python, 1200, wuhong",
                    "hb": "",
                    "sched": "slurm\ncpu|up|4|160/64/0/224",
                    "done": "",
                }}


    # -- per-server key auth -------------------------------------------------
    def op_key_generate(self, args):
        name = args[-1]
        self.state.key_present[name] = True
        return {"ok": True, "server": name, "generated": True,
                "key_material_present": True}

    def op_key_status(self, args):
        name = args[-1]
        entry = self.state.servers.get(name, {})
        return {"ok": True, "server": name,
                "auth_mode": entry.get("auth_mode", "interactive"),
                "auto_connect": bool(entry.get("auto_connect", False)),
                "key_material_present": bool(
                    self.state.key_present.get(name)),
                "batch_login_verified": bool(
                    self.state.key_verified.get(name)),
                "reconnect_state": "-", "error": ""}

    def op_key_install(self, args):
        name = args[1]
        if not self.state.key_present.get(name):
            return {"ok": False, "error": {"code": "key_missing",
                                           "message": "generate first"}}
        if getattr(self.state, "key_reject", False):
            return {"ok": False, "error": {"code": "key_verify_failed",
                                           "message": "key_rejected"}}
        entry = self.state.servers[name]
        entry["auth_mode"] = "key"
        entry["auto_connect"] = True
        self.state.key_verified[name] = True
        return {"ok": True, "server": name, "auth_mode": "key",
                "auto_connect": True, "batch_login_verified": True}

    def op_key_disable(self, args):
        name = args[-1]
        entry = self.state.servers.get(name, {})
        entry["auth_mode"] = "interactive"
        entry["auto_connect"] = False
        return {"ok": True, "server": name, "auth_mode": "interactive"}

    def op_key_revoke(self, args):
        name = args[1]
        confirm = args[args.index("--confirm-server") + 1]             if "--confirm-server" in args else ""
        if confirm != name:
            return {"ok": False, "error": {"code": "confirm_mismatch",
                                           "message": "confirm mismatch"}}
        self.state.key_present[name] = False
        entry = self.state.servers.get(name, {})
        entry["auth_mode"] = "interactive"
        entry["auto_connect"] = False
        return {"ok": True, "server": name, "revoked": True,
                "lines_removed": 1}


from pathlib import PurePosixPath  # noqa: E402  (used above)


@pytest.fixture()
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "vaspilot-home"
    home.mkdir()
    monkeypatch.setenv("VASPILOT_HOME", str(home))
    monkeypatch.delenv("VASPILOT_IDENTITY_FILE", raising=False)
    (home / "settings.json").write_text(json.dumps(
        {"vlab": {"host": "vlab.invalid", "user": "tester", "port": 22,
                  "identity_file": str(tmp_path / "id_test.pem")}}),
        encoding="utf-8")
    identity = tmp_path / "id_test.pem"
    identity.write_text("FAKEPEM\n", encoding="utf-8")
    return home


@pytest.fixture()
def fake_state(tmp_path):
    state = FakeGatewayState(tmp_path)
    state.add_server("cl9")
    state.add_server("pbs1", root="/hpc/home/tester/pbs-root", scheduler="pbs")
    # a completed+converged case and an unconverged case
    good = tmp_path / "case-good"
    state.files["cl9"][f"{ROOT}/runs/good/INCAR"] = \
        b"SYSTEM=good\nNSW=99\nNELM=60\n"
    state.files["cl9"][f"{ROOT}/runs/good/OSZICAR"] = (
        b"       N       E                     dE             d eps       ncg     rms\n"
        b"   1 F= -.10000000E+02  E0= -.10000000E+02  d E =-.100E-09\n")
    state.files["cl9"][f"{ROOT}/runs/good/OUTCAR"] = b"reached required accuracy\n"
    state.files["cl9"][f"{ROOT}/runs/bad/INCAR"] = b"NSW=99\nNELM=2\n"
    # NELM=2 exceeded: two electronic rows before the ionic close, and the
    # OUTCAR never reports convergence
    state.files["cl9"][f"{ROOT}/runs/bad/OSZICAR"] = (
        b"      1     -0.1000E+02\n      2     -0.1000E+02\n"
        b"   1 F= -.10000000E+02  E0= -.10000000E+02\n")
    state.files["cl9"][f"{ROOT}/runs/bad/OUTCAR"] = b"no accuracy line\n"
    state.files["cl9"][f"{ROOT}/runs/bad/CONTCAR"] = b"structure\n"
    return state


@pytest.fixture()
def app_with_fake(config_home, fake_state, monkeypatch):
    """An App wired to the FakeTransport (no network, no ssh)."""
    from vaspilot.cli.main import App
    from vaspilot.core.config import Config, ServerEntry

    config = Config(config_home)
    config.upsert_server(ServerEntry(name="cl9", target="user@cl9", port=22,
                                     remote_root=ROOT, persist="8h",
                                     scheduler="slurm"))
    config.upsert_server(ServerEntry(name="pbs1", target="user@pbs1", port=22,
                                     remote_root="/hpc/home/tester/pbs-root",
                                     persist="8h", scheduler="pbs"))
    config.set_default_server("cl9")
    app = App(config)
    transport = FakeTransport(fake_state)
    monkeypatch.setattr(App, "transport", lambda self: transport)
    app._transport = transport
    app._client = None
    return app, transport


@pytest.fixture()
def vasp_inputs(tmp_path):
    directory = tmp_path / "inputs"
    directory.mkdir()
    (directory / "INCAR").write_text(
        "SYSTEM = test\nENCUT = 520\nNSW = 99\nNELM = 60\nIBRION = 2\n",
        encoding="utf-8")
    (directory / "KPOINTS").write_text(
        "k-points\n0\nGamma\n4 4 4\n0 0 0\n", encoding="utf-8")
    (directory / "POSCAR").write_text(
        "test\n1.0\n3.0 0 0\n0 3.0 0\n0 0 3.0\nNa Cl\n1 1\ndirect\n"
        "0 0 0\n0.5 0.5 0.5\n", encoding="utf-8")
    (directory / "POTCAR").write_text(
        "  TITEL  = PAW Na_sv 08Apr2002\n   ENMAX =  302.0\n", encoding="utf-8")
    return directory

#!/usr/bin/env python3
"""远端控制智能体 Workspace Gateway（部署在 Vlab）。

此程序是“完整工作区模式”的唯一控制面。它只接受结构化子命令，使用
Vlab 上已登记的服务器、每服务器专用 SSH 密钥和严格的 known_hosts，
把一个已允许的远端计算目录映射到一个独立的 rclone VFS 挂载目录。

它刻意不做三件事：

* 不接受任意 shell 文本；
* 不把私钥、密码或 TOTP 写入工作区状态；
* 不对目标服务器安装 VS Code Server（VS Code Server 只会由 Remote-SSH
  安装在 Vlab）。

每次调用只输出一个 JSON 文档，适合由本机 ``huwei workspace`` CLI 调用。
仅依赖 Python 标准库；运行环境是 Vlab/Linux，而不是目标计算集群。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


VERSION = "0.1.0"
SERVER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
TARGET_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}@"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,253}$"
)
SAFE_PATH = re.compile(r"^/[A-Za-z0-9._/+@=-]{1,259}$")
WORKSPACE_RE = re.compile(r"^ws-[0-9a-f]{8}$")

DEFAULTS: dict[str, Any] = {
    "vfs_cache_mode": "writes",
    "vfs_write_back": "2s",
    "vfs_cache_max_size": "1GiB",
    "vfs_cache_max_age": "30m",
    "vfs_cache_min_free_space": "2GiB",
    "dir_cache_time": "10s",
    "attr_timeout": "1s",
    "buffer_size": "4MiB",
    "space_warning_percent": 80,
    "space_block_percent": 90,
    "single_file_warn_bytes": 32 * 1024 * 1024,
    "single_file_read_only_bytes": 256 * 1024 * 1024,
}
DENY_TEXT_NAMES = {"WAVECAR", "CHGCAR", "AECCAR0", "AECCAR2"}


class GatewayError(Exception):
    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emit(payload: dict[str, Any]) -> int:
    payload.setdefault("ok", True)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def fail(exc: GatewayError) -> int:
    return emit({"ok": False, "error": {"code": exc.code,
                                           "message": exc.message,
                                           **exc.detail}})


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2,
                      sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name)
        raise


def _load_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return dict(fallback)
    except (OSError, ValueError) as exc:
        raise GatewayError("state_unreadable",
                           f"cannot read {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise GatewayError("state_unreadable", f"{path.name} is not JSON object")
    return data


def _path_inside(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or root in path.parents


def _overlap(a: PurePosixPath, b: PurePosixPath) -> bool:
    return _path_inside(a, b) or _path_inside(b, a)


def _safe_remote_path(raw: str) -> PurePosixPath:
    if not SAFE_PATH.fullmatch(raw or ""):
        raise GatewayError("invalid_path", "远端路径只允许绝对路径和安全字符")
    result = PurePosixPath(raw)
    if "." in result.parts or ".." in result.parts:
        raise GatewayError("invalid_path", "远端路径不允许 . 或 ..")
    return result


def _run(argv: list[str], *, timeout: int = 45,
         input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, input=input_text, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise GatewayError("dependency_missing", f"程序不存在：{argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GatewayError("timeout", f"操作超时：{argv[0]}") from exc


class WorkspaceGateway:
    """Vlab 上的工作区状态、租约和 rclone 挂载管理器。"""

    def __init__(self, root: Path | None = None,
                 vaspilot_config: Path | None = None) -> None:
        self.root = (root or Path(os.environ.get(
            "HUWEI_WORKSPACE_ROOT", "~/.huwei-agent/workspaces"))).expanduser()
        self.state_path = self.root / "state.json"
        self.config_path = self.root / "config.json"
        self.lock_path = self.root / ".lock"
        self.vaspilot_config = (vaspilot_config or Path(os.environ.get(
            "VASPILOT_GATEWAY_CONFIG", "~/.config/vaspilot/servers.json"))).expanduser()

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        """Serialize state transitions. Linux uses flock; test platforms
        without fcntl still retain atomic state replacement."""
        self.root.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+", encoding="utf-8")
        try:
            try:
                import fcntl  # type: ignore
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - Windows test import
                pass
            yield
        finally:
            try:
                import fcntl  # type: ignore
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover
                pass
            handle.close()

    def settings(self) -> dict[str, Any]:
        cfg = _load_json(self.config_path, DEFAULTS)
        merged = dict(DEFAULTS)
        merged.update({key: value for key, value in cfg.items()
                       if key in DEFAULTS})
        return merged

    def _state(self) -> dict[str, Any]:
        state = _load_json(self.state_path, {"version": 1, "workspaces": {}})
        if not isinstance(state.get("workspaces"), dict):
            raise GatewayError("state_unreadable", "state workspaces is invalid")
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        _atomic_json(self.state_path, state)

    def _servers(self) -> dict[str, dict[str, Any]]:
        raw = _load_json(self.vaspilot_config, {"servers": {}})
        servers = raw.get("servers", {})
        if not isinstance(servers, dict):
            raise GatewayError("catalog_unreadable", "Vlab server catalog is invalid")
        return {str(name): dict(entry) for name, entry in servers.items()
                if isinstance(entry, dict)}

    def _server(self, name: str) -> tuple[str, dict[str, Any]]:
        if not SERVER_RE.fullmatch(name or ""):
            raise GatewayError("invalid_server", "server ID is invalid")
        entry = self._servers().get(name)
        if entry is None:
            raise GatewayError("unknown_server", f"未注册服务器：{name}")
        target = str(entry.get("target") or "")
        root = str(entry.get("remote_root") or "")
        if not TARGET_RE.fullmatch(target):
            raise GatewayError("invalid_server", f"服务器 {name} 的 target 无效")
        configured_root = _safe_remote_path(root)
        if str(configured_root) in ("/", "/home"):
            raise GatewayError("unsafe_root",
                               "服务器允许根目录不能是 / 或整个 /home")
        return name, entry

    @staticmethod
    def _key_path(server: str) -> Path:
        return Path.home() / ".ssh" / "vaspilot" / server

    @staticmethod
    def _known_hosts_path() -> Path:
        return Path.home() / ".ssh" / "known_hosts"

    def _key_ready(self, server: str, entry: dict[str, Any]) -> Path:
        if entry.get("auth_mode") != "key" or not entry.get("auto_connect"):
            raise GatewayError(
                "key_auth_required",
                f"{server} 尚未完成 Vlab 专用密钥登录；先执行服务器密钥配置后才能创建完整工作区"
            )
        key = self._key_path(server)
        if not key.is_file():
            raise GatewayError("key_missing", f"Vlab 上缺少 {server} 的专用私钥")
        known_hosts = self._known_hosts_path()
        if not known_hosts.is_file():
            raise GatewayError("known_hosts_missing",
                               "Vlab known_hosts 不存在；拒绝在未验证主机密钥时挂载")
        return key

    def _validate_workspace_path(self, server: str, entry: dict[str, Any],
                                 raw_path: str, *, check_remote: bool = True) -> str:
        path = _safe_remote_path(raw_path)
        root = _safe_remote_path(str(entry.get("remote_root") or ""))
        if not _path_inside(path, root):
            raise GatewayError("outside_allowed_root",
                               f"路径必须位于已登记根目录 {root} 内")
        if str(path) in ("/", "/home"):
            raise GatewayError("unsafe_root", "禁止挂载 / 或整个 /home")
        if check_remote:
            resolved = self._remote_realpath(server, entry, str(path))
            resolved_root = self._remote_realpath(server, entry, str(root))
            if not _path_inside(PurePosixPath(resolved), PurePosixPath(resolved_root)):
                raise GatewayError("symlink_escape",
                                   "远端路径经 realpath 后超出允许根目录")
            return resolved
        return str(path)

    def _ssh_argv(self, server: str, entry: dict[str, Any], command: str) -> list[str]:
        target = str(entry["target"])
        port = int(entry.get("port", 22) or 22)
        return [
            "ssh", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
            "-o", f"UserKnownHostsFile={self._known_hosts_path()}",
            "-o", "ConnectTimeout=15", "-i", str(self._key_path(server)),
            "-p", str(port), target, command,
        ]

    def _remote_realpath(self, server: str, entry: dict[str, Any], path: str) -> str:
        # ``path`` passed this module's strict POSIX whitelist; the shell
        # shape is fixed and includes no user-supplied executable or flags.
        import shlex
        self._key_ready(server, entry)
        result = _run(self._ssh_argv(server, entry,
                                     f"realpath -e -- {shlex.quote(path)}"), timeout=35)
        value = (result.stdout or "").strip()
        if result.returncode != 0 or not value.startswith("/"):
            raise GatewayError("remote_path_unavailable",
                               f"无法验证远端路径 {path} 是否存在且可访问")
        return value.splitlines()[0].strip()

    def _remote_permissions(self, server: str, entry: dict[str, Any], path: str) -> dict[str, bool]:
        import shlex
        command = (
            f"test -d -- {shlex.quote(path)} && "
            f"test -r -- {shlex.quote(path)} && echo read=yes || echo read=no; "
            f"test -w -- {shlex.quote(path)} && echo write=yes || echo write=no"
        )
        result = _run(self._ssh_argv(server, entry, command), timeout=35)
        text = (result.stdout or "")
        return {"read": "read=yes" in text, "write": "write=yes" in text}

    @staticmethod
    def _free_port() -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
        sock.close()
        return port

    def _rclone_bin(self) -> str:
        binary = shutil.which("rclone")
        if not binary:
            raise GatewayError("rclone_missing", "Vlab 未安装 rclone")
        return binary

    @staticmethod
    def _fuse_bin() -> str | None:
        return shutil.which("fusermount3") or shutil.which("fusermount")

    def _check_mount_capability(self) -> dict[str, Any]:
        rclone = shutil.which("rclone")
        fuse = self._fuse_bin()
        return {
            "rclone": bool(rclone),
            "rclone_path": rclone or "",
            "fuse": bool(fuse and Path("/dev/fuse").exists()),
            "fusermount": fuse or "",
            "dev_fuse": Path("/dev/fuse").exists(),
        }

    def _space(self) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(self.root)
        percent = round(usage.used * 100 / usage.total, 1) if usage.total else 100.0
        return {"used_bytes": usage.used, "free_bytes": usage.free,
                "total_bytes": usage.total, "used_percent": percent}

    def _workspace_paths(self, server: str, workspace_id: str) -> dict[str, Path]:
        base = self.root / server / workspace_id
        return {"base": base, "mount": base / "mount", "cache": base / "cache",
                "config": base / "rclone.conf", "log": base / "rclone.log",
                "code_workspace": base / "workspace.code-workspace"}

    @staticmethod
    def _remote_name() -> str:
        return "workspace"

    def _write_rclone_config(self, paths: dict[str, Path], server: str,
                             entry: dict[str, Any]) -> None:
        user, host = str(entry["target"]).split("@", 1)
        lines = [
            f"[{self._remote_name()}]", "type = sftp", f"host = {host}",
            f"user = {user}", f"port = {int(entry.get('port', 22) or 22)}",
            f"key_file = {self._key_path(server)}",
            f"known_hosts_file = {self._known_hosts_path()}", "",
        ]
        paths["config"].write_text("\n".join(lines), encoding="utf-8")
        os.chmod(paths["config"], 0o600)

    def _write_vscode_workspace(self, paths: dict[str, Path], data: dict[str, Any]) -> None:
        settings = {
            "files.autoSave": "off",
            "files.watcherExclude": {f"**/{name}": True for name in
                                     ["WAVECAR", "CHGCAR", "AECCAR0", "AECCAR2", "vasprun.xml"]},
            "search.exclude": {f"**/{name}": True for name in
                               ["WAVECAR", "CHGCAR", "AECCAR0", "AECCAR2"]},
            "huwei-bridge.workspace": {
                "workspaceId": data["workspace_id"], "server": data["server"],
                "remotePath": data["remote_path"], "mode": data["mode"],
            },
        }
        payload = {"folders": [{"path": str(paths["mount"])}], "settings": settings}
        paths["code_workspace"].write_text(json.dumps(
            payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _probe_rclone_sftp(self, server: str, entry: dict[str, Any],
                           remote_path: str) -> tuple[bool, str]:
        """Read-only rclone SFTP handshake used by ``doctor``.  This checks
        the actual backend/key/known_hosts combination, not merely OpenSSH.
        """
        try:
            rclone = self._rclone_bin()
        except GatewayError as exc:
            return False, exc.message
        probe_dir = Path(tempfile.mkdtemp(prefix=".huwei-sftp-probe-", dir=str(self.root)))
        try:
            paths = {"config": probe_dir / "rclone.conf"}
            self._write_rclone_config(paths, server, entry)
            result = _run([rclone, "--config", str(paths["config"]), "lsd",
                           f"{self._remote_name()}:{remote_path}"], timeout=45)
            if result.returncode == 0:
                return True, "rclone SFTP read-only handshake succeeded"
            return False, (result.stderr or result.stdout or "rclone SFTP failed").strip()[:300]
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)

    def _rclone_mount_argv(self, data: dict[str, Any]) -> list[str]:
        settings = self.settings()
        paths = {name: Path(value) for name, value in data["paths"].items()}
        source = f"{self._remote_name()}:{data['remote_path']}"
        argv = [
            self._rclone_bin(), "--config", str(paths["config"]), "mount", source,
            str(paths["mount"]), "--daemon", "--daemon-wait", "30s",
            "--cache-dir", str(paths["cache"]), "--dir-cache-time",
            str(settings["dir_cache_time"]), "--attr-timeout", str(settings["attr_timeout"]),
            "--buffer-size", str(settings["buffer_size"]), "--rc", "--rc-no-auth",
            "--rc-addr", f"127.0.0.1:{data['rc_port']}", "--log-file", str(paths["log"]),
            "--log-level", "NOTICE",
        ]
        # 这些大 VASP 数据默认不暴露给完整工作区，避免文件监视器、搜索或
        # 编辑器意外读取。关键输入仍建议用带 SHA 冲突检查的安全编辑模式。
        for name in sorted(DENY_TEXT_NAMES):
            argv += ["--exclude", f"**/{name}"]
        if data["mode"] == "read-only":
            argv += ["--read-only", "--vfs-cache-mode", "off"]
        else:
            argv += [
                "--vfs-cache-mode", str(settings["vfs_cache_mode"]),
                "--vfs-write-back", str(settings["vfs_write_back"]),
                "--vfs-cache-max-size", str(settings["vfs_cache_max_size"]),
                "--vfs-cache-max-age", str(settings["vfs_cache_max_age"]),
                "--vfs-cache-min-free-space", str(settings["vfs_cache_min_free_space"]),
            ]
        return argv

    @staticmethod
    def _is_mounted(path: Path) -> bool:
        mountpoint = shutil.which("mountpoint")
        if mountpoint:
            return _run([mountpoint, "-q", str(path)], timeout=5).returncode == 0
        return os.path.ismount(path)

    @staticmethod
    def _rc(port: int, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(port)}/{endpoint.lstrip('/')}",
            data=json.dumps(payload or {}).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                data = json.loads(response.read().decode("utf-8"))
                return data if isinstance(data, dict) else None
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def _lease_conflict(self, state: dict[str, Any], server: str,
                        remote_path: str, workspace_id: str = "") -> dict[str, Any] | None:
        candidate = PurePosixPath(remote_path)
        for wid, item in state["workspaces"].items():
            if wid == workspace_id or item.get("server") != server:
                continue
            if item.get("mode") != "read-write":
                continue
            if item.get("status") not in {"open", "recovering", "sync_pending", "sync_unknown"}:
                continue
            if _overlap(candidate, PurePosixPath(str(item.get("remote_path") or "/"))):
                return {"workspace_id": wid, "remote_path": item.get("remote_path"),
                        "status": item.get("status")}
        return None

    def doctor(self, *, server: str = "", path: str = "") -> dict[str, Any]:
        capability = self._check_mount_capability()
        space = self._space()
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, detail: str = "", *, required: bool = True) -> None:
            checks.append({"name": name, "ok": bool(ok), "required": required,
                           "detail": detail[:300]})

        check("vlab_os", sys.platform.startswith("linux"),
              f"{platform.platform()} glibc={platform.libc_ver()[1] or 'unknown'}")
        check("rclone", capability["rclone"], capability["rclone_path"])
        check("fuse", capability["fuse"],
              f"fusermount={capability['fusermount'] or '-'} /dev/fuse={capability['dev_fuse']}")
        check("vlab_space", space["free_bytes"] >= 0,
              f"free={space['free_bytes']} used={space['used_percent']}%")
        code_servers = list((Path.home() / ".vscode-server" / "bin").glob("*/bin/code-server"))
        runnable = False
        for candidate in code_servers[:3]:
            try:
                runnable = _run([str(candidate), "--version"], timeout=10).returncode == 0
            except GatewayError:
                continue
            if runnable:
                break
        check("vscode_server_on_vlab", runnable,
              "已可运行" if runnable else "尚未由最新版 VS Code 安装；首次连接 Vlab 时安装",
              required=False)
        server_report: dict[str, Any] = {}
        if server:
            name, entry = self._server(server)
            selected = path or str(entry["remote_root"])
            try:
                resolved = self._validate_workspace_path(name, entry, selected)
                perms = self._remote_permissions(name, entry, resolved)
                sftp_ok, sftp_detail = self._probe_rclone_sftp(name, entry, resolved)
                server_report = {"server": name, "path": resolved, "permissions": perms,
                                 "key_ready": True, "sftp": sftp_ok}
                check("target_sftp", sftp_ok, sftp_detail)
                check("target_write", bool(perms["write"]),
                      "target directory write permission" if perms["write"] else "只可读")
            except GatewayError as exc:
                server_report = {"server": name, "key_ready": False,
                                 "error": {"code": exc.code, "message": exc.message}}
                check("target_sftp", False, exc.message)
        return {"gateway": "远端控制智能体 Workspace Gateway", "version": VERSION,
                "checks": checks, "ok": all(row["ok"] or not row.get("required", True)
                                                 for row in checks
                                                 if row["name"] != "target_write"),
                "space": space, "server": server_report,
                "note": "doctor 只读，不创建挂载、不安装 VS Code Server。"}

    def open(self, *, server: str, remote_path: str, mode: str = "full") -> dict[str, Any]:
        if mode not in {"full", "read-only"}:
            raise GatewayError("invalid_mode", "mode 只能为 full 或 read-only")
        name, entry = self._server(server)
        resolved = self._validate_workspace_path(name, entry, remote_path)
        requested_mode = "read-write" if mode == "full" else "read-only"
        perms = self._remote_permissions(name, entry, resolved)
        if not perms["read"]:
            raise GatewayError("read_denied", "目标目录无读取权限")
        if requested_mode == "read-write" and not perms["write"]:
            raise GatewayError("write_denied", "目标目录无写入权限；可使用只读模式")
        capability = self._check_mount_capability()
        if not capability["rclone"] or not capability["fuse"]:
            raise GatewayError("workspace_unsupported",
                               "Vlab 缺少 rclone 或 FUSE；请保留安全编辑模式")
        self._key_ready(name, entry)
        with self._lock():
            state = self._state()
            if requested_mode == "read-write":
                conflict = self._lease_conflict(state, name, resolved)
                if conflict:
                    raise GatewayError("write_lease_conflict",
                                       "已有重叠的可写工作区；请关闭它或改用只读模式",
                                       conflict=conflict)
                settings = self.settings()
                space = self._space()
                if space["used_percent"] >= int(settings["space_block_percent"]):
                    raise GatewayError("vlab_space_blocked",
                                       "Vlab 工作区空间达到禁止新建阈值", space=space)
                min_free = _size_bytes(str(settings["vfs_cache_min_free_space"]))
                if space["free_bytes"] < min_free:
                    raise GatewayError("vlab_space_low",
                                       "Vlab 可用空间低于写缓存最小值", space=space)
            workspace_id = "ws-" + uuid.uuid4().hex[:8]
            paths = self._workspace_paths(name, workspace_id)
            for path in paths.values():
                if path.suffix:
                    path.parent.mkdir(parents=True, exist_ok=True)
                else:
                    path.mkdir(parents=True, exist_ok=True)
            data: dict[str, Any] = {
                "workspace_id": workspace_id, "server": name,
                "remote_path": resolved, "mode": requested_mode,
                "status": "starting", "created_at": now(), "heartbeat_at": now(),
                "last_sync_at": "", "last_error": "", "rc_port": self._free_port(),
                "paths": {key: str(value) for key, value in paths.items()},
            }
            self._write_rclone_config(paths, name, entry)
            self._write_vscode_workspace(paths, data)
            result = _run(self._rclone_mount_argv(data), timeout=45)
            if result.returncode != 0 or not self._is_mounted(paths["mount"]):
                detail = (result.stderr or result.stdout or "rclone mount failed").strip()[:300]
                data.update({"status": "needs_recovery", "last_error": detail})
                state["workspaces"][workspace_id] = data
                self._save_state(state)
                raise GatewayError("mount_failed", "rclone 挂载未就绪，缓存已保留供恢复",
                                   workspace_id=workspace_id, detail=detail)
            data["status"] = "open"
            state["workspaces"][workspace_id] = data
            self._save_state(state)
        return self.status(workspace_id=workspace_id)

    def _status_one(self, data: dict[str, Any]) -> dict[str, Any]:
        paths = {name: Path(value) for name, value in data["paths"].items()}
        mounted = self._is_mounted(paths["mount"])
        rc_stats = self._rc(int(data["rc_port"]), "vfs/stats") if mounted else None
        rc_queue = self._rc(int(data["rc_port"]), "vfs/queue") if mounted else None
        disk = (rc_stats or {}).get("diskCache") if isinstance(rc_stats, dict) else {}
        queue = (rc_queue or {}).get("queue") if isinstance(rc_queue, dict) else []
        if not isinstance(disk, dict):
            disk = {}
        if not isinstance(queue, list):
            queue = []
        space = self._space()
        cache_bytes = int(disk.get("bytesUsed") or _dir_size(paths["cache"]))
        state = str(data.get("status") or "unknown")
        last_error = str(data.get("last_error") or "")
        if state in {"open", "recovering", "sync_pending"} and not mounted:
            state = "needs_recovery"
            last_error = last_error or "挂载已消失；缓存等待恢复"
        elif mounted and data.get("mode") == "read-write" and \
                (rc_stats is None or rc_queue is None):
            # 不把“看不到写回队列”误报为已同步；close() 也会在这种状态
            # 拒绝卸载，直到 rclone RC 可被确认。
            state = "sync_unknown"
            last_error = "无法读取 rclone 写回队列；禁止安全关闭"
        elif mounted and len(queue) > 0:
            state = "sync_pending"
        elif mounted:
            state = "open"
        status = {
            "workspace_id": data["workspace_id"], "server": data["server"],
            "remote_path": data["remote_path"], "mount_path": str(paths["mount"]),
            "vscode_workspace_path": str(paths["code_workspace"]), "mode": data["mode"],
            "mounted": mounted, "status": state, "cache_bytes": cache_bytes,
            "pending_sync_files": len(queue),
            "uploads_in_progress": int(disk.get("uploadsInProgress") or 0),
            "last_sync_at": data.get("last_sync_at") or "",
            "last_error": last_error,
            "vlab_space": space, "created_at": data.get("created_at") or "",
            "heartbeat_at": data.get("heartbeat_at") or "",
        }
        if not queue and mounted and not status["last_error"]:
            status["last_sync_at"] = now()
        return status

    def status(self, *, workspace_id: str = "") -> dict[str, Any]:
        with self._lock():
            state = self._state()
            if workspace_id:
                if not WORKSPACE_RE.fullmatch(workspace_id or "") or workspace_id not in state["workspaces"]:
                    raise GatewayError("workspace_not_found", "工作区不存在")
                row = self._status_one(state["workspaces"][workspace_id])
                state["workspaces"][workspace_id].update({
                    "status": row["status"], "heartbeat_at": now(),
                    "last_sync_at": row["last_sync_at"],
                    "last_error": row["last_error"],
                })
                self._save_state(state)
                return row
            rows = [self._status_one(item) for item in state["workspaces"].values()]
            return {"workspaces": sorted(rows, key=lambda item: item["created_at"], reverse=True),
                    "space": self._space(), "settings": self.settings()}

    def list(self) -> dict[str, Any]:
        return self.status()

    def close(self, *, workspace_id: str, wait_seconds: int = 60) -> dict[str, Any]:
        if not WORKSPACE_RE.fullmatch(workspace_id or ""):
            raise GatewayError("workspace_not_found", "工作区 ID 无效")
        wait_seconds = max(1, min(int(wait_seconds), 300))
        with self._lock():
            state = self._state()
            data = state["workspaces"].get(workspace_id)
            if not data:
                raise GatewayError("workspace_not_found", "工作区不存在")
            paths = {name: Path(value) for name, value in data["paths"].items()}
            if not self._is_mounted(paths["mount"]):
                if data.get("status") not in {"closed", "abandoned"}:
                    data["status"] = "needs_recovery"
                    data["last_error"] = "挂载已消失；缓存未丢弃，请执行 workspace recover"
                    self._save_state(state)
                raise GatewayError("recovery_required", "挂载不可用，拒绝清理可能未同步的缓存")
            if data["mode"] == "read-write":
                deadline = time.monotonic() + wait_seconds
                while True:
                    queue_reply = self._rc(int(data["rc_port"]), "vfs/queue")
                    if queue_reply is None:
                        raise GatewayError("sync_status_unavailable",
                                           "无法读取 rclone 上传队列；拒绝卸载以避免丢失写缓存")
                    queue = queue_reply.get("queue") or []
                    if not queue:
                        break
                    for item in queue:
                        if isinstance(item, dict) and isinstance(item.get("id"), int):
                            self._rc(int(data["rc_port"]), "vfs/queue-set-expiry",
                                     {"id": item["id"], "expiry": -1000000000})
                    if time.monotonic() >= deadline:
                        data["status"] = "sync_pending"
                        data["last_error"] = "写回仍未完成；保留挂载和缓存，可稍后重试关闭"
                        self._save_state(state)
                        raise GatewayError("sync_pending",
                                           "仍有待同步文件，已禁止卸载", pending_files=len(queue))
                    time.sleep(1)
            unmounted = False
            for binary in ("fusermount3", "fusermount", "umount"):
                executable = shutil.which(binary)
                if not executable:
                    continue
                argv = [executable, "-u", str(paths["mount"])] if binary != "umount" \
                    else [executable, str(paths["mount"])]
                if _run(argv, timeout=25).returncode == 0:
                    unmounted = True
                    break
            if not unmounted or self._is_mounted(paths["mount"]):
                data["status"] = "sync_pending"
                data["last_error"] = "卸载失败；挂载和缓存保留"
                self._save_state(state)
                raise GatewayError("unmount_failed", "卸载失败，未丢弃缓存")
            self._rc(int(data["rc_port"]), "core/quit")
            data.update({"status": "closed", "closed_at": now(), "last_error": ""})
            self._save_state(state)
            return {"workspace_id": workspace_id, "closed": True,
                    "cache_retained": True, "mount_path": str(paths["mount"])}

    def recover(self, *, workspace_id: str = "", action: str = "list",
                confirm_workspace_id: str = "") -> dict[str, Any]:
        if action not in {"list", "retry", "keep", "discard"}:
            raise GatewayError("invalid_action", "recover action 无效")
        with self._lock():
            state = self._state()
            candidates = {wid: item for wid, item in state["workspaces"].items()
                          if item.get("status") in {"needs_recovery", "sync_pending"}}
            if not workspace_id or action == "list":
                return {"recoverable": [self._status_one(item)
                                        for item in candidates.values()]}
            if workspace_id not in candidates:
                raise GatewayError("workspace_not_recoverable", "该工作区不需要恢复")
            data = candidates[workspace_id]
            paths = {name: Path(value) for name, value in data["paths"].items()}
            if action == "keep":
                return {"workspace_id": workspace_id, "cache_path": str(paths["cache"]),
                        "note": "缓存已保留；不会自动删除或上传。"}
            if action == "discard":
                if confirm_workspace_id != workspace_id:
                    raise GatewayError("confirm_mismatch",
                                       "放弃恢复必须再次完整输入 workspace ID")
                if self._is_mounted(paths["mount"]):
                    raise GatewayError("workspace_still_mounted", "工作区仍挂载，拒绝丢弃缓存")
                shutil.rmtree(paths["cache"], ignore_errors=False)
                paths["cache"].mkdir(parents=True, exist_ok=True)
                data.update({"status": "abandoned", "abandoned_at": now(),
                             "last_error": "用户已明确放弃本地写缓存"})
                self._save_state(state)
                return {"workspace_id": workspace_id, "discarded": True}
            # retry: recreate precisely the recorded VFS, with the same cache
            # directory and config, so rclone can retry its persisted uploads.
            name, entry = self._server(str(data["server"]))
            self._key_ready(name, entry)
            self._validate_workspace_path(name, entry, str(data["remote_path"]))
            if self._is_mounted(paths["mount"]):
                raise GatewayError("workspace_still_mounted", "工作区已挂载，无需恢复")
            data["rc_port"] = self._free_port()
            result = _run(self._rclone_mount_argv(data), timeout=45)
            if result.returncode != 0 or not self._is_mounted(paths["mount"]):
                data["status"] = "needs_recovery"
                data["last_error"] = (result.stderr or result.stdout or "恢复挂载失败").strip()[:300]
                self._save_state(state)
                raise GatewayError("recovery_mount_failed", "恢复挂载失败；缓存仍保留")
            data.update({"status": "recovering", "last_error": "", "heartbeat_at": now()})
            self._save_state(state)
            return self._status_one(data)

    def cleanup(self, *, apply: bool = False, confirm: str = "") -> dict[str, Any]:
        with self._lock():
            state = self._state()
            candidates = []
            for wid, data in state["workspaces"].items():
                if data.get("status") not in {"closed", "abandoned"}:
                    continue
                paths = {name: Path(value) for name, value in data["paths"].items()}
                candidates.append({"workspace_id": wid, "server": data.get("server"),
                                   "cache_path": str(paths["cache"]),
                                   "cache_bytes": _dir_size(paths["cache"])})
            if not apply:
                return {"dry_run": True, "candidates": candidates,
                        "note": "仅列出已关闭会话的本地缓存；不会删除目标服务器文件。"}
            if confirm != "CLEANUP-CLOSED-WORKSPACES":
                raise GatewayError("confirm_required",
                                   "清理必须传入 --confirm CLEANUP-CLOSED-WORKSPACES")
            removed = []
            for item in candidates:
                data = state["workspaces"][item["workspace_id"]]
                paths = {name: Path(value) for name, value in data["paths"].items()}
                if self._is_mounted(paths["mount"]):
                    continue
                shutil.rmtree(paths["base"], ignore_errors=False)
                del state["workspaces"][item["workspace_id"]]
                removed.append(item)
            self._save_state(state)
            return {"dry_run": False, "removed": removed,
                    "note": "只删除 Vlab 本地缓存、日志和关闭会话；未删除任何目标服务器文件。"}


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    try:
        for base, _dirs, files in os.walk(path):
            for filename in files:
                with contextlib.suppress(OSError):
                    total += (Path(base) / filename).stat().st_size
    except OSError:
        return total
    return total


def _size_bytes(value: str) -> int:
    match = re.fullmatch(r"([0-9]+)(KiB|MiB|GiB|TiB)", value or "")
    if not match:
        return 2 * 1024 * 1024 * 1024
    factors = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
    return int(match.group(1)) * factors[match.group(2)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="huwei-workspace-gateway")
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("version")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--server", default="")
    doctor.add_argument("--path", default="")
    open_p = sub.add_parser("open")
    open_p.add_argument("--server", required=True)
    open_p.add_argument("--path", required=True)
    open_p.add_argument("--mode", default="full", choices=["full", "read-only"])
    status = sub.add_parser("status")
    status.add_argument("--workspace", default="")
    sub.add_parser("list")
    close = sub.add_parser("close")
    close.add_argument("--workspace", required=True)
    close.add_argument("--wait", type=int, default=60)
    recover = sub.add_parser("recover")
    recover.add_argument("--workspace", default="")
    recover.add_argument("--action", default="list", choices=["list", "retry", "keep", "discard"])
    recover.add_argument("--confirm-workspace-id", default="")
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--apply", action="store_true")
    cleanup.add_argument("--confirm", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    gateway = WorkspaceGateway()
    try:
        if args.operation == "version":
            return emit({"gateway": "远端控制智能体 Workspace Gateway",
                         "version": VERSION})
        if args.operation == "doctor":
            return emit(gateway.doctor(server=args.server, path=args.path))
        if args.operation == "open":
            return emit(gateway.open(server=args.server, remote_path=args.path, mode=args.mode))
        if args.operation == "status":
            return emit(gateway.status(workspace_id=args.workspace))
        if args.operation == "list":
            return emit(gateway.list())
        if args.operation == "close":
            return emit(gateway.close(workspace_id=args.workspace, wait_seconds=args.wait))
        if args.operation == "recover":
            return emit(gateway.recover(workspace_id=args.workspace, action=args.action,
                                        confirm_workspace_id=args.confirm_workspace_id))
        if args.operation == "cleanup":
            return emit(gateway.cleanup(apply=args.apply, confirm=args.confirm))
        raise GatewayError("unknown_operation", args.operation)
    except GatewayError as exc:
        return fail(exc)


if __name__ == "__main__":
    sys.exit(main())

"""Read-only Vlab login checks and explicit, visible terminal launch."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import subprocess

from ..core.errors import RemoteError, ValidationError

CHECK_TIMEOUT = 12
_MARKER = "RSCI_VLAB_CONNECTED"


def _validate(settings: dict, *, require_identity: bool = True) -> dict:
    settings = dict(settings)
    host = str(settings.get("host") or "").strip()
    user = str(settings.get("user") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:%_-]*", host):
        raise ValidationError("请填写有效的 Vlab 主机名或 IP 地址。")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", user):
        raise ValidationError("请填写有效的 Vlab SSH 用户名。")
    raw_port = str(settings.get("port", 22))
    if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
        raise ValidationError("Vlab 端口必须是 1 到 65535 之间的整数。")
    identity = str(settings.get("identity_file") or "").strip()
    if any(char in identity for char in ("\x00", "\r", "\n")):
        raise ValidationError("Vlab 私钥文件路径无效。")
    if identity:
        identity = str(Path(identity).expanduser())
        if require_identity and not Path(identity).is_file():
            raise ValidationError("Vlab 私钥文件不存在，请重新选择 PEM 文件并保存。")
    # An empty identity is supported: OpenSSH may use its agent/default key.
    settings.update(host=host, user=user, port=int(raw_port), identity_file=identity)
    return settings


def _settings(config) -> dict:
    return _validate({**config.vlab, "identity_file": config.identity_file()})


def _ssh_args(settings: dict, *, interactive: bool) -> list[str]:
    args = ["ssh", "-tt" if interactive else "-T",
            "-p", str(settings["port"]), "-l", settings["user"],
            "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
            "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1",
            "-o", f"ServerAliveInterval={30 if interactive else 3}",
            "-o", f"ServerAliveCountMax={4 if interactive else 1}"]
    if settings["identity_file"]:
        args += ["-i", settings["identity_file"]]
    if not interactive:
        args += ["-o", "BatchMode=yes", "-o", "NumberOfPasswordPrompts=0"]
    args += ["--", settings["host"]]
    return args


def _failure_message(stderr: str) -> str:
    low = stderr.lower()
    if any(value in low for value in (
            "host key verification failed", "host key has changed",
            "remote host identification has changed")):
        return "Vlab 主机密钥未被信任或已变更，请核实服务器指纹后再连接。"
    if "permission denied" in low or "authentication failed" in low:
        return "Vlab SSH 认证失败，请检查用户名、PEM 私钥或在终端完成登录。"
    if "could not resolve hostname" in low or "name or service not known" in low:
        return "无法解析 Vlab 主机名，请检查主机地址和网络/VPN。"
    if "timed out" in low or "timeout" in low:
        return "Vlab SSH 连接超时，请检查校园网/VPN、主机地址和端口。"
    if "connection refused" in low:
        return "Vlab 拒绝 SSH 连接，请检查端口和服务器 SSH 服务。"
    if any(value in low for value in ("no route to host", "network is unreachable")):
        return "无法到达 Vlab，请检查校园网/VPN 和网络连接。"
    return "Vlab SSH 连接未成功，请检查连接设置，或进入终端查看登录提示。"


def check_connection(config) -> dict:
    """Authenticate and print a fixed marker; never invoke/deploy a gateway."""
    connected = False
    try:
        settings = _settings(config)
        cmd = _ssh_args(settings, interactive=False) + [f"printf '{_MARKER}\\n'"]
        result = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=CHECK_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        connected = result.returncode == 0 and _MARKER in result.stdout.splitlines()
        message = ("已通过 SSH 登录 Vlab。" if connected
                   else _failure_message(result.stderr or ""))
    except ValidationError as exc:
        message = str(exc)
    except (TypeError, ValueError):
        message = "Vlab 连接设置无效，请检查主机、用户名和端口后保存。"
    except subprocess.TimeoutExpired:
        message = f"Vlab SSH 检测在 {CHECK_TIMEOUT} 秒后超时，请检查校园网/VPN 和服务器状态。"
    except FileNotFoundError:
        message = "本机未找到 SSH 程序，请安装或启用 OpenSSH 客户端。"
    except OSError as exc:
        message = f"无法启动 Vlab SSH 检测：{exc}"
    return {"ok": True, "connected": connected, "message": message,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def open_terminal(config) -> dict:
    """Only the user-triggered action opens a visible interactive console."""
    settings = _settings(config)
    ssh = shutil.which("ssh")
    if not ssh:
        raise RemoteError("本机未找到 SSH 程序，请安装或启用 OpenSSH 客户端。")
    args = _ssh_args(settings, interactive=True)
    args[0] = ssh
    if os.name == "nt":
        powershell = shutil.which("powershell.exe")
        if not powershell:
            raise RemoteError("未找到 Windows PowerShell，无法打开 Vlab 终端。")
        # Encode a script with individually quoted literals. No configuration
        # value passes through cmd.exe (%, &, parentheses, and $ stay literal).
        quote = lambda value: "'" + value.replace("'", "''") + "'"
        script = "& " + " ".join(quote(arg) for arg in args)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        cmd = [powershell, "-NoLogo", "-NoProfile", "-NoExit",
               "-EncodedCommand", encoded]
    else:
        terminal = shutil.which("x-terminal-emulator") or shutil.which("xterm")
        if not terminal:
            raise RemoteError("未找到可用的系统终端，请安装 xterm 后重试。")
        cmd = [terminal, "-e", *args]
    try:
        subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    except OSError as exc:
        raise RemoteError(f"无法打开 Vlab 终端：{exc}") from exc
    return {"ok": True, "opened": True, "message": "已打开 Vlab 终端，请在新窗口查看登录结果。"}


def save_connection(app, body: dict) -> dict:
    """Apply new settings to shared clients without discarding workflow state."""
    changes = {key: body[key] for key in
               ("host", "user", "port", "identity_file", "auto_deploy_gateway")
               if key in body and body[key] is not None}
    # Existing callers send blank host/user for a PEM-only update.
    for key in ("host", "user"):
        if key in changes and not str(changes[key]).strip():
            del changes[key]
    candidate = _validate({**app.config.vlab, **changes}, require_identity=False)
    saved = app.config.set_vlab(**candidate)
    app._transport = None
    replacement = app.transport()
    if app._client is not None:
        # Engine, tasks and registry all hold this same facade. Replace only
        # its Vlab transport, leaving direct sessions and running work intact.
        app._client.transport = replacement
    return {"ok": True, "saved": saved}

"""SSH transport from the local CLI to the Vlab gateway host.

The transports own every ``ssh``/``scp`` invocation. The default transport
executes the gateway on Vlab; :class:`DirectSshTransport` can execute the same
restricted helper on a selected target. Their runners are injectable so
offline tests can simulate the gateway without any network.

Failure classification (fail closed everywhere):

  - outer Vlab auth expired  -> :class:`AuthRequiredError` (exit 3, the CLI
    reports ``auth_required`` and never tries to type a password itself)
  - host key verification    -> hard :class:`RemoteError`; known_hosts is
    never modified automatically
  - anything else            -> :class:`RemoteError` with captured stderr
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Callable

from ..core.errors import AuthRequiredError, RemoteError, ValidationError
from ..core.validation import valid_server_name

# stderr fragments that mean "the reusable session is gone / never existed".
_AUTH_HINTS = (
    "permission denied",
    "authentication failed",
    "no such identity",
    "control socket",
    "connection refused",
    "connection closed by remote host",
    "connection timed out",
    "broken pipe",
    "multiplexing support is disabled",
    "disconnected",
)

# Auto-deployment only accepts a simple home-relative or absolute POSIX path.
# The value is used in a remote shell command; rejecting metacharacters keeps
# the convenience path from becoming an arbitrary shell command sink.
_SAFE_GATEWAY_PATH = re.compile(r"^(?:~/|/)[A-Za-z0-9._/-]+$")


def default_runner(cmd: list[str], *, timeout: int, capture: bool = True,
                   tty: bool = False, env: dict | None = None):
    """Run one subprocess; UTF-8 everywhere, never raises for non-zero exit."""
    kwargs: dict = {"timeout": timeout}
    if env and env.get("SSH_ASKPASS_REQUIRE") == "force":
        # Force OpenSSH to consult SSH_ASKPASS instead of attempting to read a
        # password from the hosting process' console/stdin.
        kwargs["stdin"] = subprocess.DEVNULL
    if capture:
        kwargs.update(capture_output=True, text=True,
                      encoding="utf-8", errors="replace")
        if tty:
            kwargs["stderr"] = subprocess.STDOUT
    result = subprocess.run(cmd, env=env, **kwargs)
    if capture:
        return result.returncode, result.stdout or "", result.stderr or ""
    return result.returncode, "", ""


Runner = Callable[..., tuple[int, str, str]]


class SshTransport:
    """Executes gateway operations on the Vlab host over SSH."""

    def __init__(self, *, host: str, user: str, port: int = 22,
                 identity_file: str = "", gateway_path: str = "~/bin/vaspilot-gateway",
                 workspace_gateway_path: str = "~/bin/huwei-workspace-gateway",
                 auto_deploy_gateway: bool = True,
                 runner: Runner | None = None) -> None:
        if not host or not user:
            raise ValidationError("vlab host and user are required")
        self.host = host
        self.user = user
        self.port = int(port)
        self.identity_file = identity_file
        self.gateway_path = gateway_path
        self.workspace_gateway_path = workspace_gateway_path
        self.auto_deploy_gateway = bool(auto_deploy_gateway)
        self.runner = runner or default_runner
        self._gateway_ready = False
        self._gateway_lock = threading.Lock()

    # -- command construction -----------------------------------------------
    def _base(self, *, batch: bool = True, tty: bool = False) -> list[str]:
        cmd = ["ssh", "-q", "-p", str(self.port)]
        if self.identity_file:
            cmd += ["-i", self.identity_file]
        if batch:
            cmd += ["-o", "BatchMode=yes"]
        if tty:
            cmd += ["-tt"]
        # heartbeat keepalives so idle NAT/firewall sessions survive
        cmd += ["-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4"]
        cmd += ["-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
                f"{self.user}@{self.host}"]
        return cmd

    # -- classification -------------------------------------------------------
    def _classify(self, rc: int, stdout: str, stderr: str) -> None:
        if rc == 0:
            return
        low = (stderr or "").lower()
        if "host key verification failed" in low or "host key has changed" in low:
            raise RemoteError(
                "Vlab host key verification failed; the gateway fails closed "
                "and will not modify known_hosts automatically",
                detail={"stderr": stderr.strip()[:300]})
        if rc == 255 and any(hint in low for hint in _AUTH_HINTS):
            raise AuthRequiredError(
                "the Vlab SSH session is unavailable; run "
                "'vaspilot server connect' in a terminal and enter the "
                "password/TOTP there")
        if rc == 127:
            raise RemoteError(
                "Vlab 网关程序不存在或不可执行（rc=127）；已尝试自动部署，"
                "请检查 Vlab SSH 权限或手动运行 scripts\\install_vlab_gateway.py",
                detail={"stderr": (stderr or stdout).strip()[:300]})
        raise RemoteError(f"gateway ssh failed (rc={rc})",
                          detail={"stderr": (stderr or stdout).strip()[:300]})

    def _run(self, cmd: list[str], *, timeout: int = 180,
             capture: bool = True, tty: bool = False):
        """Run one outer Vlab command and turn hangs into a useful error."""
        try:
            return self.runner(cmd, timeout=timeout, capture=capture, tty=tty)
        except subprocess.TimeoutExpired as exc:
            raise RemoteError(
                f"Vlab SSH 命令在 {timeout} 秒后超时；请确认校园网/VPN、"
                "Vlab PEM 和 Vlab 主机状态",
                detail={"command": " ".join(str(x) for x in cmd[:8])}) from exc

    def _gateway_path(self) -> str:
        path = str(self.gateway_path or "").strip()
        if not _SAFE_GATEWAY_PATH.fullmatch(path):
            raise ValidationError(
                "gateway_path 只能是简单的 ~/ 或 / 开头 POSIX 路径")
        return path

    def _probe_gateway(self, *, timeout: int = 30) -> bool:
        """Return whether the configured helper is executable on Vlab."""
        path = self._gateway_path()
        rc, stdout, stderr = self._run(
            self._base() + [f"test -x {path}"], timeout=timeout)
        if rc == 0:
            return True
        # ``test`` returns 1 for a missing/non-executable path.  Treat 127 as
        # missing as well so a partially prepared home can be repaired.
        if rc in (1, 127):
            return False
        self._classify(rc, stdout, stderr)
        return False

    def _deploy_gateway(self, *, timeout: int = 180) -> None:
        """Stage, validate and atomically install the local gateway helper."""
        source = Path(__file__).with_name("vaspilot_gateway.py")
        if not source.is_file():
            raise RemoteError(f"gateway source is missing: {source}")
        remote_path = self._gateway_path()
        stage = f"/tmp/vaspilot-gateway-{uuid.uuid4().hex[:16]}.new"
        prepare = self._base() + [
            "mkdir -p ~/bin ~/.config/vaspilot ~/.cache/vaspilot && "
            "chmod 700 ~/bin ~/.config/vaspilot ~/.cache/vaspilot"]
        rc, stdout, stderr = self._run(prepare, timeout=timeout)
        self._classify(rc, stdout, stderr)

        copy = ["scp", "-q", "-O", "-P", str(self.port)]
        if self.identity_file:
            copy += ["-i", self.identity_file]
        copy += ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                 "-o", "UpdateHostKeys=no", str(source),
                 f"{self.user}@{self.host}:{stage}"]
        rc, stdout, stderr = self._run(copy, timeout=timeout)
        self._classify(rc, stdout, stderr)

        install = self._base() + [
            f"sed -i 's/\\r//' {stage} && chmod 700 {stage} && "
            f"python3 -m py_compile {stage} && "
            f"mv -f -- {stage} {remote_path} && {remote_path} version"]
        try:
            rc, stdout, stderr = self._run(install, timeout=timeout)
            self._classify(rc, stdout, stderr)
        except BaseException:
            # Best effort cleanup; never hide the original validation/error.
            try:
                self._run(self._base() + [f"rm -f -- {stage}"], timeout=30)
            except Exception:
                pass
            raise

    def _ensure_gateway(self, *, timeout: int = 180) -> bool:
        """Check the Vlab helper and deploy it once when it is missing."""
        if not self.auto_deploy_gateway:
            return False
        with self._gateway_lock:
            if self._gateway_ready:
                return False
            if self._probe_gateway(timeout=min(30, max(5, timeout))):
                self._gateway_ready = True
                return False
            self._deploy_gateway(timeout=timeout)
            self._gateway_ready = True
            return True

    # -- gateway JSON protocol -------------------------------------------------
    def run_gateway(self, args: list[str], *, timeout: int = 180,
                    tty: bool = False, capture: bool = True) -> dict:
        """Invoke one gateway operation; return its parsed JSON document."""
        self._ensure_gateway(timeout=timeout)
        import shlex
        quoted = " ".join(shlex.quote(str(a)) for a in args)
        cmd = self._base(batch=not tty, tty=tty) + [
            f"{self.gateway_path} {quoted}"]
        rc, stdout, stderr = self._run(cmd, timeout=timeout,
                                       capture=capture, tty=tty)
        self._classify(rc, stdout, stderr)
        if not capture:
            return {"ok": True}
        return self.parse_gateway_document(stdout, stderr)

    def run_workspace_gateway(self, args: list[str], *, timeout: int = 180) -> dict:
        """Call the Vlab-only Workspace Gateway using the same verified outer
        SSH route as ordinary gateway operations.  The workspace component
        owns rclone/FUSE; this transport never opens a target-HPC SSH route.
        """
        import shlex
        quoted = " ".join(shlex.quote(str(a)) for a in args)
        cmd = self._base() + [f"{self.workspace_gateway_path} {quoted}"]
        rc, stdout, stderr = self._run(cmd, timeout=timeout, capture=True)
        self._classify(rc, stdout, stderr)
        return self.parse_gateway_document(stdout, stderr)

    @staticmethod
    def parse_gateway_document(stdout: str, stderr: str = "") -> dict:
        text = (stdout or "").strip()
        if not text:
            raise RemoteError("gateway produced no output",
                              detail={"stderr": (stderr or "").strip()[:300]})
        # exactly one JSON document per invocation; tolerate leading noise
        start = text.find("{")
        if start < 0:
            raise RemoteError("gateway output was not JSON",
                              detail={"stdout": text[:300]})
        try:
            document = json.loads(text[start:])
        except json.JSONDecodeError as exc:
            raise RemoteError(f"gateway JSON is malformed: {exc}",
                              detail={"stdout": text[:300]}) from exc
        if not isinstance(document, dict):
            raise RemoteError("gateway JSON was not an object")
        if not document.get("ok") and isinstance(document.get("error"), dict):
            code = str(document["error"].get("code", "remote_error"))
            message = str(document["error"].get("message", "gateway error"))
            if code == "disconnected":
                raise AuthRequiredError(
                    f"server session required: {message}")
            raise RemoteError(message, detail={"gateway_code": code})
        return document

    # -- interactive terminal ----------------------------------------------------
    def open_connect_terminal(self, *, server: str, target: str,
                              port: int = 22, persist: str = "8h") -> dict:
        """Open a *visible* terminal that lands IMMEDIATELY on the target
        server's password prompt (e.g. ``jlyang@…'s password:``).

        Fast path: instead of hopping through the gateway script (outer ssh
        → python startup → its own ssh), run ONE outer shell that checks the
        mux socket and either reports already-connected or spawns the
        ControlMaster right away. The human types password/TOTP only here;
        the master session is identical to what every gateway operation
        reuses afterwards (same socket path ~/.cache/vaspilot/ctl-<srv>.sock).

        Windows note: spawn ``cmd /k ssh …`` directly with
        CREATE_NEW_CONSOLE — never via ``start``, which would treat the
        joined command line as one file name. /k keeps the window open so
        the OTP prompt/retry/result stays readable.
        """
        import re as _re

        valid_server_name(server)
        if not _re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}@[A-Za-z0-9][A-Za-z0-9._-]{0,253}",
                target or ""):
            raise ValidationError("target must be user@host")
        if not _re.fullmatch(r"(?i)(yes|no|[0-9]+[smhdw])", persist or "8h"):
            raise ValidationError("persist must be yes/no or like 8h, 30m")
        port = int(port)

        sock = f"$HOME/.cache/vaspilot/ctl-{server}.sock"
        remote = (
            f'mkdir -p "$HOME/.cache/vaspilot"; '
            f'if [ -S "{sock}" ] && ssh -o BatchMode=yes '
            f'-o "ControlPath={sock}" -O check "{target}" >/dev/null 2>&1; then '
            f'echo "{server}: session alive (nothing to do)"; '
            f"else rm -f \"{sock}\"; "
            f'exec ssh -M -S "{sock}" -o ControlMaster=yes '
            f'-o "ControlPersist={persist}" '
            f"-o ServerAliveInterval=30 -o ServerAliveCountMax=4 "
            f"-o StrictHostKeyChecking=ask -o UpdateHostKeys=no "
            f"-o NumberOfPasswordPrompts=3 -p {port} -fN \"{target}\"; fi"
        )
        outer = ["ssh", "-tt", "-p", str(self.port)]
        if self.identity_file:
            outer += ["-i", self.identity_file]
        outer += ["-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
                  f"{self.user}@{self.host}", remote]
        cmd = ["cmd", "/k"] + outer
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        try:
            subprocess.Popen(cmd, creationflags=creationflags)
        except OSError as exc:
            raise RemoteError(f"could not open a login terminal: {exc}") from exc
        return {"opened": True, "server": server,
                "note": "enter the password and TOTP in the new terminal window"}

    def interactive_connect(self, server: str) -> dict:
        """Run the interactive connect in the CURRENT terminal (CLI mode)."""
        self._ensure_gateway(timeout=600)
        inner = f"{self.gateway_path} connect --server {server}"
        cmd = self._base(batch=False, tty=True) + [inner]
        rc, _, _ = self.runner(cmd, timeout=600, capture=False)
        if rc != 0:
            raise AuthRequiredError(
                f"interactive connect for {server} did not complete; "
                "check the password/TOTP in the terminal output")
        return {"connected": True, "server": server}

    # -- staged file motion -------------------------------------------------------
    def stage_path(self) -> str:
        return f"/tmp/vaspilot-{uuid.uuid4().hex[:16]}"

    def scp_to_stage(self, local_path: str | Path, stage: str,
                     *, timeout: int = 600) -> None:
        cmd = ["scp", "-q", "-P", str(self.port)]
        if self.identity_file:
            cmd += ["-i", self.identity_file]
        cmd += ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "UpdateHostKeys=no", str(local_path),
                f"{self.user}@{self.host}:{stage}"]
        rc, stdout, stderr = self.runner(cmd, timeout=timeout)
        self._classify(rc, stdout, stderr)

    def scp_from_stage(self, stage: str, local_path: str | Path,
                       *, timeout: int = 600) -> None:
        cmd = ["scp", "-q", "-P", str(self.port)]
        if self.identity_file:
            cmd += ["-i", self.identity_file]
        cmd += ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "UpdateHostKeys=no",
                f"{self.user}@{self.host}:{stage}", str(local_path)]
        rc, stdout, stderr = self.runner(cmd, timeout=timeout)
        self._classify(rc, stdout, stderr)

    def rm_stage(self, stage: str) -> None:
        cmd = self._base() + [f"rm -f -- {stage}"]
        try:
            rc, stdout, stderr = self.runner(cmd, timeout=60)
            self._classify(rc, stdout, stderr)
        except AuthRequiredError:
            pass  # stage cleanup is best effort; Vlab /tmp cleans up anyway

    def probe_reachable(self, *, timeout: int = 15) -> tuple[bool, str]:
        """Cheap reachability probe used by ``server doctor``."""
        cmd = ["ssh", "-q", "-p", str(self.port)]
        if self.identity_file:
            cmd += ["-i", self.identity_file]
        cmd += ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
                f"{self.user}@{self.host}", "echo VASPILOT_OK"]
        try:
            rc, stdout, stderr = self.runner(cmd, timeout=timeout)
        except (RemoteError, AuthRequiredError):
            return False, "ssh invocation failed"
        if rc == 0 and "VASPILOT_OK" in stdout:
            return True, "ssh reached the gateway host"
        return False, (stderr or "no output").strip()[:200]


class DirectSshTransport(SshTransport):
    """Run the same restricted gateway helper directly on one target host.

    The normal transport SSHes to Vlab and invokes ``vaspilot-gateway``
    there.  Direct mode uploads the standalone helper to the selected target
    once, then runs it with ``VASPILOT_DIRECT_MODE=1``.  The helper executes
    its already-validated fixed command shapes locally on that target, so the
    client keeps the same path checks, trash handling and audit semantics
    without requiring a Vlab hop.
    """

    def __init__(self, *, server: str, target: str, port: int = 22,
                 identity_file: str = "", remote_root: str = "",
                 persist: str = "8h", scheduler: str = "auto",
                 auth_mode: str = "interactive", auto_connect: bool = False,
                 password: str = "", totp_seed: str = "",
                 credentials_due: bool = False,
                 runner: Runner | None = None) -> None:
        # Do not call SshTransport.__init__: direct mode has no Vlab host.
        self.server = valid_server_name(server)
        if not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}@[A-Za-z0-9][A-Za-z0-9._-]{0,253}",
                target or ""):
            raise ValidationError("target must be user@host")
        self.target = target
        self.port = int(port)
        self.identity_file = identity_file
        self.remote_root = remote_root
        self.persist = persist or "8h"
        self.scheduler = scheduler or "auto"
        self.auth_mode = auth_mode or "interactive"
        self.auto_connect = bool(auto_connect)
        self.password = str(password or "")
        self.totp_seed = str(totp_seed or "")
        self.credentials_due = bool(credentials_due)
        self.runner = runner or default_runner
        self._helper_ready = False
        # Native Windows OpenSSH does not implement the Unix-domain control
        # sockets used by ControlMaster/ControlPath.  Passing those options
        # causes the opaque ``getsockname failed: Not a socket`` error before
        # authentication even starts.  Keep multiplexing on POSIX hosts, but
        # deliberately use ordinary one-shot SSH on Windows; unattended
        # authentication can use the DPAPI-backed SSH_ASKPASS bridge or a
        # normal key/agent.
        self._multiplex = os.name != "nt"
        control = Path.home() / ".vaspilot" / f"direct-ctl-{self.server}.sock"
        control.parent.mkdir(parents=True, exist_ok=True)
        self.control_path = str(control).replace("\\", "/")
        self.helper_path = "$HOME/.cache/vaspilot/vaspilot-gateway-direct.py"

    def _base(self, *, batch: bool = True, tty: bool = False) -> list[str]:
        cmd = ["ssh", "-q", "-p", str(self.port)]
        if self.identity_file:
            cmd += ["-i", self.identity_file]
        if batch:
            cmd += self._auth_options(tty=tty)
        if tty:
            cmd += ["-tt"]
        cmd += ["-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=4",
                "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no"]
        if self._multiplex:
            cmd += ["-o", f"ControlPath={self.control_path}"]
        cmd += [self.target]
        return cmd

    def _auth_options(self, *, tty: bool = False) -> list[str]:
        use_saved = bool(self.password) and not self.credentials_due and not tty
        if not use_saved:
            return ["-o", "BatchMode=yes"]
        return ["-o", "BatchMode=no",
                "-o", "KbdInteractiveAuthentication=yes",
                "-o", "PasswordAuthentication=yes",
                "-o", "PreferredAuthentications=keyboard-interactive,password,publickey",
                "-o", "NumberOfPasswordPrompts=1"]

    def _run(self, cmd: list[str], *, timeout: int = 180,
             capture: bool = True, tty: bool = False):
        """Run SSH with a short-lived askpass environment when authorised."""
        use_saved = bool(self.password) and not self.credentials_due and not tty
        if not use_saved:
            return self.runner(cmd, timeout=timeout, capture=capture, tty=tty)
        from ..core.credentials import askpass_env
        with askpass_env(self.password, self.totp_seed) as env:
            try:
                return self.runner(cmd, timeout=timeout, capture=capture,
                                   tty=tty, env=env)
            except TypeError as exc:
                # Preserve compatibility with tiny test runners written for
                # the old Runner signature; production accepts env.
                if "env" not in str(exc):
                    raise
                return self.runner(cmd, timeout=timeout, capture=capture, tty=tty)

    def _classify(self, rc: int, stdout: str, stderr: str) -> None:
        if rc == 0:
            return
        low = (stderr or "").lower()
        if "host key verification failed" in low or "host key has changed" in low:
            raise RemoteError(
                "target host key verification failed; refusing to modify known_hosts",
                detail={"stderr": stderr.strip()[:300]})
        if rc == 255 and any(hint in low for hint in _AUTH_HINTS):
            raise AuthRequiredError(
                "the direct SSH session is unavailable; run the server's "
                "direct login terminal and enter the password/TOTP there")
        if rc == 127:
            raise RemoteError(
                "直连目标服务器缺少 python3 或无法执行直连网关助手；"
                "请确认目标环境提供 Python 3",
                detail={"stderr": (stderr or stdout).strip()[:300]})
        raise RemoteError(f"direct ssh failed (rc={rc})",
                          detail={"stderr": (stderr or stdout).strip()[:300]})

    def _ensure_helper(self, *, timeout: int = 180) -> None:
        if self._helper_ready:
            return
        source = Path(__file__).with_name("vaspilot_gateway.py")
        if not source.is_file():
            raise RemoteError(f"direct gateway source is missing: {source}")
        stage = f"/tmp/vaspilot-direct-{uuid.uuid4().hex[:16]}.py"
        prepare = self._base() + [
            'mkdir -p "$HOME/.cache/vaspilot" && '
            'chmod 700 "$HOME/.cache/vaspilot"']
        rc, stdout, stderr = self._run(prepare, timeout=timeout)
        self._classify(rc, stdout, stderr)
        # OpenSSH 9+ defaults to the SFTP protocol for scp.  A number of
        # legacy HPC login nodes (including CentOS 7 installations) expose
        # the SSH service but do not provide a working sftp subsystem; they
        # close the channel with the unhelpful ``scp: Connection closed``.
        # Direct-route transfers therefore use the legacy SCP protocol.
        copy = ["scp", "-q", "-O", "-P", str(self.port)]
        if self.identity_file:
            copy += ["-i", self.identity_file]
        copy += self._auth_options()
        copy += ["-o", "StrictHostKeyChecking=yes",
                 "-o", "UpdateHostKeys=no"]
        if self._multiplex:
            copy += ["-o", f"ControlPath={self.control_path}"]
        copy += [str(source), f"{self.target}:{stage}"]
        rc, stdout, stderr = self._run(copy, timeout=timeout)
        self._classify(rc, stdout, stderr)
        install = self._base() + [
            f"cp -- {stage} {self.helper_path} && chmod 700 {self.helper_path} && "
            f"rm -f -- {stage} && python3 -m py_compile {self.helper_path}"]
        rc, stdout, stderr = self._run(install, timeout=timeout)
        self._classify(rc, stdout, stderr)
        self._helper_ready = True

    def _direct_command(self, args: list[str]) -> str:
        import shlex
        values = {
            "VASPILOT_DIRECT_MODE": "1",
            "VASPILOT_DIRECT_SERVER": self.server,
            "VASPILOT_DIRECT_TARGET": self.target,
            "VASPILOT_DIRECT_PORT": str(self.port),
            "VASPILOT_DIRECT_ROOT": self.remote_root,
            "VASPILOT_DIRECT_PERSIST": self.persist,
            "VASPILOT_DIRECT_SCHEDULER": self.scheduler,
            "VASPILOT_DIRECT_AUTH_MODE": self.auth_mode,
            "VASPILOT_DIRECT_AUTO_CONNECT": "1" if self.auto_connect else "0",
        }
        env = " ".join(f"{key}={shlex.quote(value)}"
                       for key, value in values.items())
        quoted = " ".join(shlex.quote(str(arg)) for arg in args)
        return f"{env} python3 {self.helper_path} {quoted}".strip()

    def run_gateway(self, args: list[str], *, timeout: int = 180,
                    tty: bool = False, capture: bool = True) -> dict:
        self._ensure_helper(timeout=timeout)
        cmd = self._base(batch=not tty, tty=tty) + [self._direct_command(args)]
        rc, stdout, stderr = self._run(cmd, timeout=timeout,
                                       capture=capture, tty=tty)
        self._classify(rc, stdout, stderr)
        if not capture:
            return {"ok": True}
        return self.parse_gateway_document(stdout, stderr)

    def run_workspace_gateway(self, args: list[str], *, timeout: int = 180) -> dict:
        raise RemoteError(
            "完整工作区模式仍需要 Vlab；直连服务器请使用文件/作业/安全编辑功能")

    def scp_to_stage(self, local_path: str | Path, stage: str,
                     *, timeout: int = 600) -> None:
        # See _ensure_helper: keep direct transfers on the legacy SCP
        # protocol for old login nodes without a usable SFTP subsystem.
        cmd = ["scp", "-q", "-O", "-P", str(self.port)]
        if self.identity_file:
            cmd += ["-i", self.identity_file]
        cmd += self._auth_options()
        cmd += ["-o", "StrictHostKeyChecking=yes",
                "-o", "UpdateHostKeys=no"]
        if self._multiplex:
            cmd += ["-o", f"ControlPath={self.control_path}"]
        cmd += [str(local_path), f"{self.target}:{stage}"]
        rc, stdout, stderr = self._run(cmd, timeout=timeout)
        self._classify(rc, stdout, stderr)

    def scp_from_stage(self, stage: str, local_path: str | Path,
                       *, timeout: int = 600) -> None:
        # See _ensure_helper: keep direct transfers on the legacy SCP
        # protocol for old login nodes without a usable SFTP subsystem.
        cmd = ["scp", "-q", "-O", "-P", str(self.port)]
        if self.identity_file:
            cmd += ["-i", self.identity_file]
        cmd += self._auth_options()
        cmd += ["-o", "StrictHostKeyChecking=yes",
                "-o", "UpdateHostKeys=no"]
        if self._multiplex:
            cmd += ["-o", f"ControlPath={self.control_path}"]
        cmd += [f"{self.target}:{stage}", str(local_path)]
        rc, stdout, stderr = self._run(cmd, timeout=timeout)
        self._classify(rc, stdout, stderr)

    def rm_stage(self, stage: str) -> None:
        try:
            rc, stdout, stderr = self._run(
                self._base() + [f"rm -f -- {stage}"], timeout=60)
            self._classify(rc, stdout, stderr)
        except (AuthRequiredError, RemoteError):
            pass

    def interactive_connect(self, server: str) -> dict:
        if server != self.server:
            raise ValidationError("direct transport server mismatch")
        rc, stdout, stderr = self._run(
            self._base(batch=False, tty=True), timeout=600,
            capture=False, tty=True)
        self._classify(rc, stdout, stderr)
        return {"connected": True, "server": server, "route": "direct"}

    def open_connect_terminal(self, *, server: str, target: str,
                              port: int = 22, persist: str = "8h") -> dict:
        if server != self.server or target != self.target or int(port) != self.port:
            raise ValidationError("direct transport connection details changed")
        cmd = ["cmd", "/k", "ssh", "-tt", "-p", str(self.port)]
        if self._multiplex:
            cmd += ["-M", "-S", self.control_path,
                    "-o", "ControlMaster=yes",
                    "-o", f"ControlPersist={persist or '8h'}"]
        cmd += ["-o", "StrictHostKeyChecking=ask", "-o", "UpdateHostKeys=no"]
        if self.identity_file:
            cmd += ["-i", self.identity_file]
        cmd += [self.target]
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        try:
            subprocess.Popen(cmd, creationflags=creationflags)
        except OSError as exc:
            raise RemoteError(f"could not open a direct login terminal: {exc}") from exc
        note = "enter the target server password/TOTP in the new terminal"
        interactive_only = False
        if not self._multiplex:
            note += "；原生 Windows 不支持 SSH 会话复用，后台直连可用加密凭据或 SSH 密钥/代理"
            interactive_only = True
        return {"opened": True, "server": server, "route": "direct",
                "note": note, "interactive_only": interactive_only}

    def open_key_setup_terminal(self, *, server: str, target: str,
                                port: int = 22) -> dict:
        """Open a visible PowerShell helper that installs a local public key.

        Credentials are typed only into the SSH process in the new console;
        the web server never receives them.  The helper creates the standard
        ``id_ed25519`` key when neither it nor ``id_rsa`` exists, appends the
        public key to the target's ``authorized_keys``, then verifies a
        BatchMode login.  This is intentionally a direct-route operation and
        never contacts Vlab.
        """
        if server != self.server or target != self.target or int(port) != self.port:
            raise ValidationError("direct transport connection details changed")

        def ps_quote(value: str) -> str:
            return "'" + str(value).replace("'", "''") + "'"

        ssh_dir = Path.home() / ".ssh"
        candidates = [ssh_dir / "id_ed25519", ssh_dir / "id_rsa"]
        private = next((path for path in candidates if path.is_file()),
                       candidates[0])
        priv = ps_quote(str(private))
        host = ps_quote(self.target)
        port_text = ps_quote(str(self.port))
        script = "\n".join([
            "$ErrorActionPreference = 'Stop'",
            f"$priv = {priv}",
            "$sshDir = Split-Path -Parent $priv",
            "New-Item -ItemType Directory -Force -Path $sshDir | Out-Null",
            "if (-not (Test-Path -LiteralPath $priv)) {",
            "  Write-Host '未找到本机 SSH 密钥，正在生成 id_ed25519…'",
            "  & ssh-keygen -q -t ed25519 -N '' -f $priv",
            "  if ($LASTEXITCODE -ne 0) { throw 'ssh-keygen 生成密钥失败' }",
            "}",
            "$pub = $priv + '.pub'",
            "if (-not (Test-Path -LiteralPath $pub)) { throw '公钥文件不存在：' + $pub }",
            "$pub64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($pub))",
            "Write-Host ''",
            "Write-Host '=== vasp 直连密钥配置 ===' -ForegroundColor Cyan",
            "Write-Host ('目标服务器：' + " + host + ")",
            "Write-Host '操作顺序：' -ForegroundColor Yellow",
            "Write-Host '  1) 第一次出现 Are you sure you want to continue connecting 时输入 yes。'",
            "Write-Host '  2) 出现 Password: 时输入目标服务器登录密码，然后按 Enter。'",
            "Write-Host '  3) 出现 Verification code / One-time password / OTP 时，输入当前 6 位 Google 验证码，然后按 Enter。'",
            "Write-Host '  4) 配置结束后窗口会自动验证公钥登录；看到 VASPILOT_KEY_OK 即成功。'",
            "Write-Host '密码和验证码只输入在此 SSH 窗口，不要粘贴到网页。' -ForegroundColor DarkYellow",
            "Write-Host ''",
            "& ssh -tt -p " + port_text +
            " -o StrictHostKeyChecking=ask -o UpdateHostKeys=no " + host +
            " \"umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; " +
            "printf '%s' '$pub64' | base64 -d >> ~/.ssh/authorized_keys; " +
            "chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys\"",
            "if ($LASTEXITCODE -ne 0) { throw '公钥安装失败，请检查登录提示和目标目录权限' }",
            "Write-Host '正在验证 SSH 公钥登录…'",
            "& ssh -p " + port_text + " -o BatchMode=yes -o StrictHostKeyChecking=yes " +
            "-o UpdateHostKeys=no " + host + " 'printf VASPILOT_KEY_OK'",
            "if ($LASTEXITCODE -eq 0) { Write-Host '直连密钥配置成功，网页稍后会显示已连接。' }",
            "else { Write-Host '公钥已写入，但验证失败；请确认目标 SSH 服务允许公钥认证。' }",
            "Read-Host '按回车关闭此窗口'",
            "Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue",
        ])
        # Do not put this multi-line script in ``cmd /k ... -Command``:
        # Windows command-line quoting treats the embedded quotes/newlines as
        # delimiters and leaves only an idle ``E:\Projects\vaspilot>`` prompt.
        # A temporary script has no credentials or private material and is
        # removed by the script itself after the operator presses Enter.
        script_path = Path(tempfile.gettempdir()) / \
            f"vaspilot-direct-key-{uuid.uuid4().hex[:16]}.ps1"
        try:
            # Windows PowerShell 5.1 detects UTF-8 reliably only with a BOM;
            # without it the Chinese guidance is decoded as the local ANSI
            # code page and the generated script can fail to parse.
            script_path.write_bytes(b"\xef\xbb\xbf" +
                                    script.replace("\n", "\r\n").encode("utf-8"))
        except OSError as exc:
            raise RemoteError(f"could not prepare direct key setup script: {exc}") from exc
        cmd = ["cmd", "/k", "powershell.exe", "-NoLogo", "-NoProfile",
               "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        try:
            subprocess.Popen(cmd, creationflags=creationflags)
        except OSError as exc:
            try:
                script_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise RemoteError(f"could not open a direct key setup terminal: {exc}") from exc
        return {"opened": True, "server": server, "route": "direct",
                "note": "已打开直连密钥配置窗口；密码和 Google 验证码只在该窗口输入"}

    def probe_reachable(self, *, timeout: int = 15) -> tuple[bool, str]:
        cmd = self._base() + ["echo VASPILOT_OK"]
        try:
            rc, stdout, stderr = self._run(cmd, timeout=timeout)
        except (RemoteError, AuthRequiredError):
            return False, "ssh invocation failed"
        if rc == 0 and "VASPILOT_OK" in stdout:
            return True, "ssh reached the target server directly"
        return False, (stderr or "no output").strip()[:200]

"""SSH transport from the local CLI to the Vlab gateway host.

The transport owns every ``ssh``/``scp`` invocation. Its runner is injectable
so offline tests can simulate the gateway without any network.

Failure classification (fail closed everywhere):

  - outer Vlab auth expired  -> :class:`AuthRequiredError` (exit 3, the CLI
    reports ``auth_required`` and never tries to type a password itself)
  - host key verification    -> hard :class:`RemoteError`; known_hosts is
    never modified automatically
  - anything else            -> :class:`RemoteError` with captured stderr
"""

from __future__ import annotations

import json
import re
import subprocess
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


def default_runner(cmd: list[str], *, timeout: int, capture: bool = True,
                   tty: bool = False, env: dict | None = None):
    """Run one subprocess; UTF-8 everywhere, never raises for non-zero exit."""
    kwargs: dict = {"timeout": timeout}
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
                 runner: Runner | None = None) -> None:
        if not host or not user:
            raise ValidationError("vlab host and user are required")
        self.host = host
        self.user = user
        self.port = int(port)
        self.identity_file = identity_file
        self.gateway_path = gateway_path
        self.runner = runner or default_runner

    # -- command construction -----------------------------------------------
    def _base(self, *, batch: bool = True, tty: bool = False) -> list[str]:
        cmd = ["ssh", "-q", "-p", str(self.port)]
        if self.identity_file:
            cmd += ["-i", self.identity_file]
        if batch:
            cmd += ["-o", "BatchMode=yes"]
        if tty:
            cmd += ["-tt"]
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
        raise RemoteError(f"gateway ssh failed (rc={rc})",
                          detail={"stderr": (stderr or stdout).strip()[:300]})

    # -- gateway JSON protocol -------------------------------------------------
    def run_gateway(self, args: list[str], *, timeout: int = 180,
                    tty: bool = False, capture: bool = True) -> dict:
        """Invoke one gateway operation; return its parsed JSON document."""
        import shlex
        quoted = " ".join(shlex.quote(str(a)) for a in args)
        cmd = self._base(batch=not tty, tty=tty) + [
            f"{self.gateway_path} {quoted}"]
        rc, stdout, stderr = self.runner(cmd, timeout=timeout, capture=capture, tty=tty)
        self._classify(rc, stdout, stderr)
        if not capture:
            return {"ok": True}
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
    def open_connect_terminal(self, server: str) -> dict:
        """Open a *visible* terminal for interactive login (never headless).

        Passwords and TOTP are typed by the human in this window only.

        Windows note: spawn ``cmd /k ssh …`` directly with
        CREATE_NEW_CONSOLE — do NOT go through ``start``, which would treat
        the joined command line as one file name ("Windows cannot find
        'ssh -q …'"). /k keeps the window open so the connect result (job
        prompts, OTP retry, success line) stays readable.
        """
        valid_server_name(server)
        inner = f"{self.gateway_path} connect --server {server}"
        ssh_args = self._base(batch=False, tty=True) + [inner]
        cmd = ["cmd", "/k"] + ssh_args
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        try:
            subprocess.Popen(cmd, creationflags=creationflags)
        except OSError as exc:
            raise RemoteError(f"could not open a login terminal: {exc}") from exc
        return {"opened": True, "server": server,
                "note": "enter the password and TOTP in the new terminal window"}

    def interactive_connect(self, server: str) -> dict:
        """Run the interactive connect in the CURRENT terminal (CLI mode)."""
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

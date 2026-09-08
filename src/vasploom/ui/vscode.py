"""VS Code launcher for the Vlab-only complete-workspace mode.

This module creates only the local ``huwei-vlab`` Remote-SSH alias.  Target
HPC paths are exposed through Vlab rclone SFTP: the target server receives no
VS Code Server, locally installed public key, or direct Remote-SSH alias.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from ..core.errors import ValidationError

VLAB_WORKSPACE_MARK_BEGIN = "# --- Huwei Workspace Vlab block (auto-generated) ---"
VLAB_WORKSPACE_MARK_END = "# --- Huwei Workspace Vlab end ---"


# ---------------------------------------------------------------- Workspace Vlab
def workspace_vlab_alias() -> str:
    """The only Remote-SSH alias used by complete-workspace mode.

    It terminates on Vlab; rclone on Vlab reaches the target cluster.  In
    particular this function never creates an alias for ``cl12``/``minus``.
    """
    return "huwei-vlab"


def _replace_block(existing: str, block: str, begin: str, end: str) -> str:
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.S)
    if pattern.search(existing):
        return pattern.sub(lambda _m: block, existing)
    sep = "\n" if existing and not existing.endswith("\n") else ""
    return existing + sep + block


def workspace_vlab_ssh_config_block(*, vlab_host: str, vlab_user: str,
                                    vlab_port: int, identity_file: str) -> str:
    if not vlab_host or not vlab_user:
        raise ValidationError("Vlab host and user are required")
    identity = str(identity_file).replace("\\", "/")
    return (
        f"{VLAB_WORKSPACE_MARK_BEGIN}\n"
        f"Host {workspace_vlab_alias()}\n"
        f"  HostName {vlab_host}\n"
        f"  User {vlab_user}\n"
        f"  Port {int(vlab_port)}\n"
        f"  IdentityFile {identity}\n"
        "  IdentitiesOnly yes\n"
        "  StrictHostKeyChecking yes\n"
        "  UpdateHostKeys no\n"
        "  ServerAliveInterval 30\n"
        "  ServerAliveCountMax 4\n"
        f"{VLAB_WORKSPACE_MARK_END}\n"
    )


def launch_vlab_workspace(path: str, *, vlab_host: str, vlab_user: str,
                          vlab_port: int, identity_file: str,
                          config_path: Path | None = None) -> dict:
    """Open a Vlab-side ``.code-workspace`` file in current VS Code.

    The URI deliberately names only ``huwei-vlab``.  Thus the VS Code Server
    is installed/used on Vlab, while the CentOS 7 target remains untouched.
    """
    identity = Path(identity_file).expanduser()
    if not identity.is_file():
        raise ValidationError("Vlab identity file is missing; cannot open workspace")
    if not path.startswith("/"):
        raise ValidationError("Vlab workspace path must be absolute")
    block = workspace_vlab_ssh_config_block(
        vlab_host=vlab_host, vlab_user=vlab_user, vlab_port=int(vlab_port),
        identity_file=str(identity))
    cfg = config_path or (Path.home() / ".ssh" / "config")
    cfg.parent.mkdir(parents=True, exist_ok=True)
    existing = cfg.read_text(encoding="utf-8", errors="replace") if cfg.is_file() else ""
    cfg.write_text(_replace_block(existing, block, VLAB_WORKSPACE_MARK_BEGIN,
                                  VLAB_WORKSPACE_MARK_END), encoding="utf-8")
    code_cli = shutil.which("code") or shutil.which("code.cmd")
    if not code_cli:
        raise ValidationError("VS Code CLI not found (install VS Code or add 'code' to PATH)")
    uri = f"vscode-remote://ssh-remote+{workspace_vlab_alias()}{path}"
    subprocess.Popen(["cmd", "/c", code_cli, "--file-uri", uri],
                     creationflags=subprocess.CREATE_NO_WINDOW)
    return {"ok": True, "alias": workspace_vlab_alias(), "uri": uri,
            "path": path, "kind": "workspace"}

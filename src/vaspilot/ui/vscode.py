"""One-click VS Code Remote-SSH into a VASPilot-managed server.

The generated SSH alias tunnels through the Vlab host and reuses the
gateway's persistent mux socket (``ctl-<server>.sock``), so VS Code never
needs the server's password or TOTP: the master session was already
authenticated when the user connected. The alias lives in a managed block
inside the user's ``~/.ssh/config`` and is regenerated idempotently.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from ..core.errors import ValidationError

MARK_BEGIN = "# --- VASPilot managed block (auto-generated) ---"
MARK_END = "# --- VASPilot end ---"


def alias_for(server: str) -> str:
    return f"vaspilot-{server}"


def ssh_config_block(server: str, *, target: str, port: int,
                     vlab_host: str, vlab_user: str, vlab_port: int,
                     identity_file: str, vscode_identity: str = "") -> str:
    if "@" not in target:
        raise ValidationError(f"server target {target!r} must be user@host")
    user, host = target.split("@", 1)
    sock = f"$HOME/.cache/vaspilot/ctl-{server}.sock"
    proxy = (
        f'ssh -q -i "{identity_file}" -p {vlab_port} '
        f"-o BatchMode=yes -o StrictHostKeyChecking=yes "
        f'{vlab_user}@{vlab_host} '
        f'"ssh -S {sock} -o BatchMode=yes -W {host}:{port} {target}"')
    return (
        f"{MARK_BEGIN}\n"
        f"Host {alias_for(server)}\n"
        f"  HostName {host}\n"
        f"  User {user}\n"
        f"  Port {port}\n"
        + (f"  IdentityFile {vscode_identity}\n" if vscode_identity else "")
        + f"  StrictHostKeyChecking accept-new\n"
        f"  ProxyCommand {proxy}\n"
        f"{MARK_END}\n")


def local_public_key() -> tuple[Path, str]:
    """The local keypair VS Code will offer to the server: prefer an
    existing id_ed25519/id_rsa, otherwise generate a dedicated one."""
    ssh_dir = Path.home() / ".ssh"
    for name in ("id_ed25519.pub", "id_rsa.pub", "vaspilot_vscode.pub"):
        p = ssh_dir / name
        if p.is_file():
            pub = p.read_text(encoding="utf-8").strip()
            if pub.startswith(("ssh-", "ecdsa-")):
                return p.with_suffix(""), pub
    ssh_dir.mkdir(parents=True, exist_ok=True)
    priv = ssh_dir / "vaspilot_vscode"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "",
                    "-C", "vaspilot-vscode", "-f", str(priv), "-q"],
                   check=True)
    pub = (ssh_dir / "vaspilot_vscode.pub").read_text(encoding="utf-8").strip()
    return priv, pub


def install_command(pubkey: str) -> str:
    """Idempotent authorized_keys append, run through the gateway session."""
    q = pubkey.replace("'", "'\\''")
    return ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            "touch ~/.ssh/authorized_keys && "
            "chmod 600 ~/.ssh/authorized_keys && "
            "{ grep -qF '" + q + "' ~/.ssh/authorized_keys || "
            "printf '%s\\n' '" + q + "' >> ~/.ssh/authorized_keys; }")


def upsert_managed_block(config_path: Path, block: str) -> None:
    """Replace the managed block in the user's ssh config (or append it)."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if config_path.is_file():
        existing = config_path.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(
        re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n?",
        re.S)
    if pattern.search(existing):
        # lambda: block is literal — Windows paths (\U, \u) are not escapes
        updated = pattern.sub(lambda _m: block, existing)
    else:
        sep = "\n" if existing and not existing.endswith("\n") else ""
        updated = existing + sep + block
    config_path.write_text(updated, encoding="utf-8")


def launch_vscode(server: str, path: str, *, target: str, port: int,
                  vlab_host: str, vlab_user: str, vlab_port: int,
                  identity_file: str,
                  config_path: Path | None = None,
                  is_file: bool = False,
                  vscode_identity: str = "") -> dict:
    if not identity_file or not Path(identity_file).is_file():
        raise ValidationError(
            "Vlab identity file is missing; cannot build the VS Code tunnel")
    if vscode_identity and not Path(vscode_identity).is_file():
        raise ValidationError("local VS Code key is missing")
    if not path.startswith("/"):
        raise ValidationError("remote path must be absolute")
    block = ssh_config_block(
        server, target=target, port=int(port), vlab_host=vlab_host,
        vlab_user=vlab_user, vlab_port=int(vlab_port),
        identity_file=identity_file, vscode_identity=vscode_identity)
    cfg = config_path or (Path.home() / ".ssh" / "config")
    upsert_managed_block(cfg, block)
    code_cli = shutil.which("code") or shutil.which("code.cmd")
    if not code_cli:
        raise ValidationError(
            "VS Code CLI not found (install VS Code or add 'code' to PATH)")
    uri = f"vscode-remote://ssh-remote+{alias_for(server)}{path}"
    # a single file opens as an editor tab, a directory as the workspace
    flag = "--file-uri" if is_file else "--folder-uri"
    subprocess.Popen(["cmd", "/c", code_cli, flag, uri],
                     creationflags=subprocess.CREATE_NO_WINDOW)
    return {"ok": True, "alias": alias_for(server), "path": path,
            "kind": "file" if is_file else "folder"}

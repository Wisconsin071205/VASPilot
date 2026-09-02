"""VS Code Remote-SSH bridge: config block generation and launcher."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import vaspilot.ui.vscode as vs
from vaspilot.core.errors import ValidationError

def test_workspace_alias_terminates_on_vlab_only():
    block = vs.workspace_vlab_ssh_config_block(
        vlab_host="vlab.ustc.edu.cn", vlab_user="ubuntu", vlab_port=22,
        identity_file=r"C:\keys\vlab.pem")
    assert "Host huwei-vlab" in block
    assert "HostName vlab.ustc.edu.cn" in block
    assert "StrictHostKeyChecking yes" in block
    assert "ProxyCommand" not in block
    assert "cl12" not in block and "minus" not in block


def test_direct_target_launcher_is_not_exported():
    assert not hasattr(vs, "launch_vscode")
    assert not hasattr(vs, "ssh_config_block")

def test_launch_workspace_opens_vlab_uri_not_target(tmp_path, monkeypatch):
    identity = tmp_path / "vlab.pem"
    identity.write_text("FAKEPEM\n", encoding="utf-8")
    spawned = []
    monkeypatch.setattr(vs.shutil, "which", lambda name: "code.cmd")
    monkeypatch.setattr(vs.subprocess, "Popen",
                        lambda cmd, creationflags=0: spawned.append(cmd))
    result = vs.launch_vlab_workspace(
        "/home/ubuntu/.huwei-agent/workspaces/cl12/ws-a13f/workspace.code-workspace",
        vlab_host="vlab.ustc.edu.cn", vlab_user="ubuntu", vlab_port=22,
        identity_file=str(identity), config_path=tmp_path / "cfg")
    assert result["alias"] == "huwei-vlab"
    assert spawned[0][3] == "--file-uri"
    assert "ssh-remote+huwei-vlab" in spawned[0][4]
    text = (tmp_path / "cfg").read_text(encoding="utf-8")
    assert "Host huwei-vlab" in text and "cl12" not in text


def test_workspace_launch_requires_local_vlab_identity(tmp_path):
    with pytest.raises(ValidationError, match="Vlab identity"):
        vs.launch_vlab_workspace(
            "/home/ubuntu/.huwei-agent/workspaces/cl12/ws-a13f/workspace.code-workspace",
            vlab_host="vlab.ustc.edu.cn", vlab_user="ubuntu", vlab_port=22,
            identity_file=str(tmp_path / "missing.pem"), config_path=tmp_path / "cfg")

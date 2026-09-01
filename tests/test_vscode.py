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

BLOCK_ARGS = dict(target="jlyang@114.214.207.167", port=22,
                  vlab_host="vlab.ustc.edu.cn", vlab_user="ubuntu",
                  vlab_port=22, identity_file=r"C:\u\vlab.pem")


def test_block_contents():
    block = vs.ssh_config_block("minus", **BLOCK_ARGS)
    assert "Host vaspilot-minus" in block
    assert "User jlyang" in block
    assert "ctl-minus.sock" in block
    assert "-W 114.214.207.167:22 jlyang@114.214.207.167" in block
    assert "ServerAliveInterval" not in block  # tunnel, not a master


def test_upsert_is_idempotent(tmp_path):
    cfg = tmp_path / ".ssh" / "config"
    vs.upsert_managed_block(cfg, vs.ssh_config_block("minus", **BLOCK_ARGS))
    cfg.write_text(cfg.read_text(encoding="utf-8")
                   + "Host my-own-box\n  HostName 1.2.3.4\n", encoding="utf-8")
    vs.upsert_managed_block(cfg, vs.ssh_config_block("minus", **BLOCK_ARGS))
    text = cfg.read_text(encoding="utf-8")
    assert text.count("Host vaspilot-minus") == 1
    assert "Host my-own-box" in text            # user content preserved
    assert text.index("vaspilot-minus") < text.index("my-own-box")


def test_launch_writes_config_and_spawns(tmp_path, monkeypatch):
    identity = tmp_path / "id.pem"
    identity.write_text("FAKEPEM\n", encoding="utf-8")
    spawned = []

    monkeypatch.setattr(vs.shutil, "which",
                        lambda name: r"C:\code\bin\code.cmd")
    monkeypatch.setattr(vs.subprocess, "Popen",
                        lambda cmd, creationflags=0: spawned.append(cmd))
    cfg = tmp_path / "ssh-config"
    result = vs.launch_vscode(
        "minus", "/share/home/jlyang", target="jlyang@114.214.207.167",
        port=22, vlab_host="vlab.ustc.edu.cn", vlab_user="ubuntu",
        vlab_port=22, identity_file=str(identity), config_path=cfg)
    assert result["alias"] == "vaspilot-minus"
    text = cfg.read_text(encoding="utf-8")
    assert "vaspilot-minus" in text
    assert spawned and spawned[0][2].endswith("code.cmd")
    uri = spawned[0][4]
    assert uri == "vscode-remote://ssh-remote+vaspilot-minus/share/home/jlyang"


def test_launch_file_uri(tmp_path, monkeypatch):
    identity = tmp_path / "id.pem"
    identity.write_text("FAKEPEM\n", encoding="utf-8")
    spawned = []
    monkeypatch.setattr(vs.shutil, "which", lambda name: "code.cmd")
    monkeypatch.setattr(vs.subprocess, "Popen",
                        lambda cmd, creationflags=0: spawned.append(cmd))
    result = vs.launch_vscode(
        "minus", "/share/home/jlyang/runs/case/OUTCAR", target="jlyang@h",
        port=22, vlab_host="v", vlab_user="u", vlab_port=22,
        identity_file=str(identity), config_path=tmp_path / "cfg",
        is_file=True)
    assert result["kind"] == "file"
    assert spawned[0][3] == "--file-uri"
    assert spawned[0][4].endswith("/share/home/jlyang/runs/case/OUTCAR")


def test_missing_identity_rejected(tmp_path):
    with pytest.raises(ValidationError):
        vs.launch_vscode(
            "minus", "/share", target="j@h", port=22,
            vlab_host="v", vlab_user="u", vlab_port=22,
            identity_file=str(tmp_path / "nope.pem"),
            config_path=tmp_path / "cfg")


def test_relative_path_rejected(tmp_path):
    identity = tmp_path / "id.pem"
    identity.write_text("FAKEPEM\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        vs.launch_vscode(
            "minus", "share/rel", target="j@h", port=22,
            vlab_host="v", vlab_user="u", vlab_port=22,
            identity_file=str(identity), config_path=tmp_path / "cfg")

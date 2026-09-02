"""Offline tests for the Vlab-only Workspace Gateway.

No test invokes rclone, FUSE, Vlab or an HPC server.  They validate the
state/lease/path rules and command construction; real mounts remain an
environment acceptance item.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = "/hpc/home/tester/vaspilot-root"
MODULE_PATH = (Path(__file__).resolve().parents[1] / "gateway" /
               "huwei-workspace-gateway" / "huwei_workspace_gateway.py")
SPEC = importlib.util.spec_from_file_location("huwei_workspace_gateway", MODULE_PATH)
assert SPEC and SPEC.loader
gateway_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gateway_module)


@pytest.fixture()
def gateway(tmp_path):
    catalog = tmp_path / "servers.json"
    catalog.write_text(json.dumps({"servers": {"cl12": {
        "target": "tester@cl12.example.edu", "port": 22,
        "remote_root": ROOT, "auth_mode": "key", "auto_connect": True,
    }}}), encoding="utf-8")
    return gateway_module.WorkspaceGateway(root=tmp_path / "workspaces",
                                           vaspilot_config=catalog)


def test_workspace_defaults_use_writes_not_full(gateway):
    settings = gateway.settings()
    assert settings["vfs_cache_mode"] == "writes"
    assert settings["vfs_cache_max_size"] == "1GiB"
    assert settings["vfs_cache_min_free_space"] == "2GiB"


def test_only_registered_root_and_selected_subdir_are_allowed(gateway, monkeypatch):
    _name, entry = gateway._server("cl12")
    monkeypatch.setattr(gateway, "_key_ready", lambda *_: Path("/tmp/key"))
    monkeypatch.setattr(gateway, "_remote_realpath", lambda *_args: _args[-1])
    allowed = gateway._validate_workspace_path("cl12", entry, ROOT + "/01_relax")
    assert allowed.endswith("/01_relax")
    with pytest.raises(gateway_module.GatewayError, match="根目录"):
        gateway._validate_workspace_path("cl12", entry, "/home/tester")


def test_write_lease_rejects_overlapping_paths(gateway):
    state = {"workspaces": {
        "ws-a13f0001": {"server": "cl12", "remote_path": ROOT + "/calc",
                         "mode": "read-write", "status": "open"},
    }}
    conflict = gateway._lease_conflict(state, "cl12", ROOT + "/calc/01_relax")
    assert conflict and conflict["workspace_id"] == "ws-a13f0001"
    assert gateway._lease_conflict(state, "cl12", ROOT + "/other") is None


def test_mount_command_has_isolated_writes_cache_and_loopback_rc(gateway, monkeypatch, tmp_path):
    monkeypatch.setattr(gateway, "_rclone_bin", lambda: "/usr/bin/rclone")
    paths = gateway._workspace_paths("cl12", "ws-a13f0001")
    for value in paths.values():
        value.parent.mkdir(parents=True, exist_ok=True)
    data = {"workspace_id": "ws-a13f0001", "server": "cl12",
            "remote_path": ROOT + "/calc/01_relax", "mode": "read-write",
            "rc_port": 23991, "paths": {name: str(value) for name, value in paths.items()}}
    argv = gateway._rclone_mount_argv(data)
    assert "--vfs-cache-mode" in argv
    assert argv[argv.index("--vfs-cache-mode") + 1] == "writes"
    assert "full" not in argv
    assert argv[argv.index("--rc-addr") + 1] == "127.0.0.1:23991"
    assert str(paths["cache"]) in argv


def test_cleanup_is_preview_until_explicit_confirmation(gateway):
    paths = gateway._workspace_paths("cl12", "ws-a13f0001")
    paths["cache"].mkdir(parents=True, exist_ok=True)
    (paths["cache"] / "pending").write_text("x", encoding="utf-8")
    state = {"version": 1, "workspaces": {"ws-a13f0001": {
        "workspace_id": "ws-a13f0001", "server": "cl12", "status": "closed",
        "paths": {name: str(value) for name, value in paths.items()},
    }}}
    gateway._save_state(state)
    preview = gateway.cleanup()
    assert preview["dry_run"] is True
    assert preview["candidates"][0]["cache_bytes"] == 1
    with pytest.raises(gateway_module.GatewayError, match="confirm"):
        gateway.cleanup(apply=True, confirm="no")

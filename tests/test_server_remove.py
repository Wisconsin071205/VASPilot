"""Server removal from the edit dialog uses the existing catalog operation."""
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess

import pytest

from rsci.core import secretbox
from rsci.core.config import ServerEntry
from rsci.core.errors import RemoteError
from tests.conftest import ROOT
from tests.test_ui import ui, call  # noqa: F401


def test_remove_server_preserves_remote_files_and_running_jobs(ui):
    state = ui["state"]
    state.files["cl9"][f"{ROOT}/keep.txt"] = b"keep this file"
    state.jobs["cl9"].append({"job_id": "412", "state": "RUNNING"})
    files = deepcopy(state.files["cl9"])
    jobs = deepcopy(state.jobs["cl9"])

    result = call(ui, "server.remove", {"server": "cl9"})

    assert result["ok"] and result["removed"] == "cl9"
    assert ui["app"].config.load_servers() == []
    assert ui["app"].config.default_server() == ""
    assert "cl9" not in state.servers
    assert state.files["cl9"] == files
    assert state.jobs["cl9"] == jobs
    assert call(ui, "state", {"refresh": False})["servers"] == []


def test_remove_direct_server_does_not_contact_gateway_or_other_servers(ui, monkeypatch):
    config = ui["app"].config
    config.upsert_server(ServerEntry("direct1", "user@direct1", route="direct"))
    config.set_default_server("direct1")

    def unavailable(*args, **kwargs):
        pytest.fail("removing a direct server must stay local")

    monkeypatch.setattr(ui["app"].client().transport, "run_gateway", unavailable)
    result = call(ui, "server.remove", {"server": "direct1"})

    assert result["ok"] and result["removed"] == "direct1"
    assert [entry.name for entry in config.load_servers()] == ["cl9"]
    assert config.default_server() == ""
    assert "cl9" in ui["state"].servers


@pytest.mark.parametrize("name", [None, "", "bad name", "cl9;other", "missing"])
def test_remove_requires_an_explicit_registered_server(ui, name):
    result = call(ui, "server.remove", {"server": name})

    assert not result["ok"]
    assert [entry.name for entry in ui["app"].config.load_servers()] == ["cl9"]
    assert ui["app"].config.default_server() == "cl9"
    assert "cl9" in ui["state"].servers


def test_failed_remove_preserves_local_configuration(ui, monkeypatch):
    config = ui["app"].config
    before = config.servers_path.read_bytes()
    settings = config.load_settings()

    def unavailable(args, **kwargs):
        assert args == ["server-remove", "cl9"]
        raise RemoteError("Vlab 暂时不可用，请重试")

    monkeypatch.setattr(ui["app"].client().transport, "run_gateway", unavailable)
    result = call(ui, "server.remove", {"server": "cl9"})

    assert not result["ok"] and "请重试" in result["error"]["message"]
    assert config.servers_path.read_bytes() == before
    assert config.load_settings() == settings
    assert "cl9" in ui["state"].servers


@pytest.mark.skipif(not secretbox.available(), reason="credential vault uses Windows DPAPI")
@pytest.mark.parametrize("route", ["vlab", "direct"])
def test_remove_clears_saved_credentials(ui, route):
    config = ui["app"].config
    entry = ui["app"].client().server_entry("cl9")
    entry.route = route
    config.upsert_server(entry)
    config.save_server_credentials("cl9", password="test-password", totp_seed="JBSWY3DPEHPK3PXP")

    assert call(ui, "server.remove", {"server": "cl9"})["ok"]
    assert "cl9" not in config.load_settings().get("server_credentials", {})


def test_server_remove_frontend():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for browser logic tests")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests/frontend/server_remove.cjs"),
         str(root / "src/rsci/ui/static/index.html")],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stdout + result.stderr

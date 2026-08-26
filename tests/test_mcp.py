"""MCP stdio server tests: initialize, tools/list, tools/call, self-test."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
ROOT = "/hpc/home/tester/vaspilot-root"


def rpc(method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


@pytest.fixture()
def mcp_session(tmp_path, monkeypatch, fake_state):
    """In-process MCP session with the fake gateway transport injected."""
    from vaspilot.mcp import server as mcp_server
    from vaspilot.cli.main import App
    from vaspilot.core.config import Config, ServerEntry
    from tests.conftest import FakeTransport

    home = tmp_path / "mcp-home"
    home.mkdir()
    monkeypatch.setenv("VASPILOT_HOME", str(home))
    monkeypatch.delenv("VASPILOT_MCP_MODE", raising=False)
    (home / "settings.json").write_text(json.dumps(
        {"vlab": {"host": "vlab.invalid", "user": "t", "port": 22,
                  "identity_file": ""}}), encoding="utf-8")

    config = Config(home)
    config.upsert_server(ServerEntry(
        name="cl9", target="user@cl9", port=22, remote_root=ROOT,
        persist="8h", scheduler="slurm"))
    config.set_default_server("cl9")
    app = App(config)
    transport = FakeTransport(fake_state)
    monkeypatch.setattr(App, "transport", lambda self: transport)
    registry = app.registry()
    return mcp_server, app, registry


class TestMcpProtocol:
    def test_initialize(self, mcp_session):
        server, app, registry = mcp_session
        reply = server._handle(app, registry,
                               rpc("initialize",
                                   {"protocolVersion": "2024-11-05"}))
        assert reply["result"]["serverInfo"]["name"] == "vaspilot"
        assert "tools" in reply["result"]["capabilities"]

    def test_tools_list(self, mcp_session):
        server, app, registry = mcp_session
        reply = server._handle(app, registry, rpc("tools/list"))
        names = {t["name"] for t in reply["result"]["tools"]}
        assert {"server_list", "remote_list", "remote_read", "vasp_progress",
                "job_state", "open_remote_login", "open_approval_terminal",
                "vaspilot_self_check"} <= names
        for name in names:
            assert not any(word in name for word in ("shell", "exec", "bash"))

    def test_tools_call_read(self, mcp_session):
        server, app, registry = mcp_session
        reply = server._handle(app, registry, rpc("tools/call", {
            "name": "remote_read",
            "arguments": {"server": "cl9", "path": f"{ROOT}/runs/good/INCAR"},
        }))
        assert reply["result"]["isError"] is False
        payload = json.loads(reply["result"]["content"][0]["text"])
        assert payload["ok"] is True

    def test_tools_call_outside_root(self, mcp_session):
        server, app, registry = mcp_session
        reply = server._handle(app, registry, rpc("tools/call", {
            "name": "remote_read",
            "arguments": {"server": "cl9", "path": "/etc/passwd"},
        }))
        assert reply["result"]["isError"] is True
        assert "root" in reply["result"]["content"][0]["text"]

    def test_tools_call_potcar_refused(self, mcp_session):
        server, app, registry = mcp_session
        reply = server._handle(app, registry, rpc("tools/call", {
            "name": "remote_read",
            "arguments": {"server": "cl9", "path": f"{ROOT}/x/POTCAR"},
        }))
        assert reply["result"]["isError"] is True

    def test_analysis_only_blocks_write(self, mcp_session, monkeypatch):
        monkeypatch.setenv("VASPILOT_MCP_MODE", "analysis_only")
        server, app, registry = mcp_session
        reply = server._handle(app, registry, rpc("tools/call", {
            "name": "remote_mkdir",
            "arguments": {"server": "cl9", "path": f"{ROOT}/newdir"},
        }))
        assert reply["result"]["isError"] is True
        assert "analysis_only" in reply["result"]["content"][0]["text"]

    def test_unknown_tool(self, mcp_session):
        server, app, registry = mcp_session
        reply = server._handle(app, registry, rpc("tools/call", {
            "name": "run_shell", "arguments": {}}))
        assert reply["result"]["isError"] is True

    def test_ping_and_notifications(self, mcp_session):
        server, app, registry = mcp_session
        assert server._handle(app, registry, rpc("ping"))["result"] == {}
        assert server._handle(
            app, registry,
            {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    def test_self_check_tool(self, mcp_session):
        server, app, registry = mcp_session
        reply = server._handle(app, registry, rpc("tools/call", {
            "name": "vaspilot_self_check", "arguments": {}}))
        payload = json.loads(reply["result"]["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["registry_tools"] > 10

    def test_open_login_needs_valid_server_name(self, mcp_session):
        server, app, registry = mcp_session
        reply = server._handle(app, registry, rpc("tools/call", {
            "name": "open_remote_login",
            "arguments": {"server": "bad server name"}}))
        assert reply["result"]["isError"] is True


class TestMcpSelfTest:
    def test_self_test_runs(self, config_home):
        env = {**os.environ, "VASPILOT_HOME": str(config_home),
               "PYTHONPATH": str(SRC)}
        result = subprocess.run(
            [sys.executable, "-m", "vaspilot.mcp", "--self-test"],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
            env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "self-test passed" in result.stdout

    def test_stdio_framing(self, config_home):
        """The subprocess speaks newline-delimited JSON-RPC on stdio."""
        env = {**os.environ, "VASPILOT_HOME": str(config_home),
               "PYTHONPATH": str(SRC)}
        process = subprocess.Popen(
            [sys.executable, "-m", "vaspilot.mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            env=env)
        request = json.dumps(rpc("initialize", {"protocolVersion":
                                                "2024-11-05"})) + "\n"
        process.stdin.write(request)
        process.stdin.flush()
        line = process.stdout.readline()
        reply = json.loads(line)
        assert reply["result"]["serverInfo"]["name"] == "vaspilot"
        process.stdin.close()
        process.wait(timeout=30)

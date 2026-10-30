"""Connection refresh remains local-first and cancellation-safe."""
from types import SimpleNamespace

import pytest

from tests.test_ui import ui, call  # noqa: F401
from rsci.ui.server import UiHandler
from rsci.core.errors import RemoteError
from rsci.gateway.transport import SshTransport


def test_two_live_ui_servers_cannot_share_a_port(ui):
    from rsci.ui.server import _bind_with_fallback
    port = int(ui["base"].rsplit(":", 1)[1])
    second = _bind_with_fallback(UiHandler, "127.0.0.1", port)
    try:
        assert second.server_address[1] != port
    finally:
        second.server_close()


def test_local_state_never_waits_for_ssh(ui, monkeypatch):
    def unavailable(*args, **kwargs):
        raise AssertionError("local state must not contact SSH")
    monkeypatch.setattr(ui["app"].client(), "status", unavailable)
    result = call(ui, "state", {"refresh": False})
    assert result["ok"]
    assert result["servers"][0]["name"] == "cl9"
    assert result["servers"][0]["connected"] is None


def test_ui_status_does_not_reconnect_during_refresh(ui):
    ui["app"].config.save_server_credentials("cl9", password="test-password")
    ui["state"].connected["cl9"] = False
    result = call(ui, "server.status", {"server": "cl9"})
    assert not result["ok"]
    assert not ui["state"].connected["cl9"]


@pytest.mark.parametrize("error", [BrokenPipeError, ConnectionResetError, ConnectionAbortedError])
@pytest.mark.parametrize("where", ["headers", "body"])
def test_cancelled_browser_response_is_not_sent_twice(error, where):
    handler = object.__new__(UiHandler)
    handler.send_response = lambda *args: None
    handler.send_header = lambda *args: None
    def fail(*args):
        raise error("client left")
    handler.end_headers = fail if where == "headers" else lambda: None
    handler.wfile = SimpleNamespace(write=fail)
    handler._send_json({"ok": True})
    assert handler.close_connection


def test_vlab_network_timeout_is_not_a_target_login_failure():
    transport = SshTransport(host="vlab.invalid", user="ubuntu")
    assert "ConnectTimeout=10" in transport._base()
    with pytest.raises(RemoteError, match="校园网/VPN"):
        transport._classify(255, "", "ssh: connect to host vlab.invalid port 22: Connection timed out")


def test_connection_frontend():
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for browser logic tests")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / "tests/frontend/connection_refresh.cjs"),
                             str(root / "src/rsci/ui/static/index.html")],
                            capture_output=True, text=True, encoding="utf-8", timeout=20,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stdout + result.stderr

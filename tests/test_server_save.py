"""Saving a Vlab route must not log into the old route or retry timeouts."""
import json

import pytest

from tests.test_ui import ui, call  # noqa: F401
from rsci.core.errors import RemoteError
from rsci.gateway.transport import SshTransport


def test_credential_only_save_never_contacts_server(ui, monkeypatch):
    client = ui["app"].client()
    def forbidden(*args, **kwargs):
        raise AssertionError("saving credentials must stay local")
    monkeypatch.setattr(client, "status", forbidden)
    monkeypatch.setattr(client, "server_edit", forbidden)
    result = call(ui, "server.edit", {"id":"cl9", "new_name":"cl9",
                                     "credential_password":"test-password"})
    assert result["ok"] and result["local_only"]
    assert ui["app"].config.get_server_credentials("cl9")["password"] == "test-password"


def test_root_edit_skips_login_probe(ui, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("metadata save must not probe the target")
    monkeypatch.setattr(ui["app"].client(), "status", forbidden)
    result = call(ui, "server.edit", {"id":"cl9", "remote_root":"/new/root"})
    assert result["ok"]
    assert ui["app"].client().server_entry("cl9").remote_root == "/new/root"


def test_route_switch_does_one_bounded_registration(ui, monkeypatch):
    client = ui["app"].client()
    entry = client.server_entry("cl9")
    entry.route = "direct"
    entry.auto_connect = True
    ui["app"].config.upsert_server(entry)
    def forbidden(*args, **kwargs):
        raise AssertionError("route switch must not attempt direct login")
    monkeypatch.setattr(client, "status", forbidden)
    calls = []
    def register(args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": True}
    monkeypatch.setattr(client.transport, "run_gateway", register)
    result = call(ui, "server.edit", {"id":"cl9", "route":"vlab"})
    assert result["ok"]
    assert len(calls) == 1
    assert calls[0][0][0] == "server-add"
    assert "--auto-connect" in calls[0][0]
    assert calls[0][1]["timeout"] == 15
    assert client.server_entry("cl9").route == "vlab"


def test_route_switch_timeout_is_not_retried_or_saved(ui, monkeypatch):
    client = ui["app"].client()
    entry = client.server_entry("cl9")
    entry.route = "direct"
    ui["app"].config.upsert_server(entry)
    calls = []
    def fail(args, **kwargs):
        calls.append(args)
        raise RemoteError("Vlab timeout")
    monkeypatch.setattr(client.transport, "run_gateway", fail)
    result = call(ui, "server.edit", {"id":"cl9", "route":"vlab"})
    assert not result["ok"]
    assert len(calls) == 1
    assert client.server_entry("cl9").route == "direct"


def test_catalog_save_shares_budget_across_setup_and_command(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("rsci.gateway.transport.time.monotonic", lambda: clock[0])
    timeouts = []
    def runner(cmd, *, timeout, **kwargs):
        timeouts.append(timeout)
        clock[0] += 6
        return 0, json.dumps({"ok":True,"gateway_version":"1.5.4","protocol":"2"}), ""
    transport = SshTransport(host="vlab.example", user="ubuntu", runner=runner)
    assert transport.run_gateway(["server-add", "test"], timeout=15)["ok"]
    assert timeouts == [15, 9, 3]
    # Per-request budget is reset: ordinary commands retain their own timeout.
    transport.run_gateway(["servers"], timeout=60)
    assert timeouts[-1] == 60


def test_catalog_lock_wait_is_bounded():
    class BusyLock:
        def acquire(self, *, timeout):
            assert 0 < timeout <= 15
            return False
        def release(self):
            raise AssertionError("must not release a lock we did not acquire")
    transport = SshTransport(host="vlab.example", user="ubuntu")
    transport._gateway_lock = BusyLock()
    with pytest.raises(RemoteError, match="保存等待超时"):
        transport.run_gateway(["server-add", "test"], timeout=15)

    with pytest.raises(RemoteError, match="检测等待超时"):
        transport.run_gateway(["status", "--server", "test"], timeout=15)

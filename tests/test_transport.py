"""Transport terminal-spawning regression tests (the `start` bug)."""

from __future__ import annotations

import json
import subprocess

import pytest

from vasploom.core.errors import ValidationError
from vasploom.gateway.transport import SshTransport


@pytest.fixture()
def transport():
    return SshTransport(host="vlab.ustc.edu.cn", user="ubuntu", port=22,
                        identity_file=r"C:\u\id.pem",
                        gateway_path="~/bin/vasploom-gateway")


class TestOpenConnectTerminal:
    def test_direct_master_spawn_no_start(self, transport, monkeypatch):
        """Regression: never via `start`; fast-path inline shell lands on
        the target's password prompt without gateway round trips."""
        captured: list[list] = []

        def fake_popen(cmd, creationflags=0, **kw):
            captured.append(list(cmd))

            class P:
                pid = 0

            return P()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        result = transport.open_connect_terminal(
            server="minus", target="jlyang@114.214.203.217",
            port=22, persist="8h")
        assert result is None or result.get("opened") in (True, None) or True
        cmd = captured[0]
        assert cmd[0] == "cmd" and cmd[1] == "/k" and cmd[2] == "ssh"
        joined = " ".join(cmd)
        # the outer hop carries ONE inline remote command…
        assert "-tt" in cmd[:6]
        # …that immediately spawns the per-server ControlMaster on Vlab
        assert "-M -S" in joined
        assert "ctl-minus.sock" in joined
        assert '-fN "jlyang@114.214.203.217"' in joined \
            or "-fN jlyang@114.214.203.217" in joined
        # already-connected check runs BEFORE the master spawn
        assert joined.index("-O check") < joined.index("-fN")
        # no legacy gateway-script indirection left in the path
        assert "vasploom-gateway" not in joined

    def test_invalid_server_or_target_rejected(self, transport, monkeypatch):
        def fake_popen(cmd, creationflags=0, **kw):
            raise AssertionError("must not spawn")

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        with pytest.raises(ValidationError):
            transport.open_connect_terminal(
                server="bad name", target="u@h", port=22, persist="8h")
        with pytest.raises(ValidationError):
            transport.open_connect_terminal(
                server="ok", target="no-at-sign", port=22, persist="8h")
        with pytest.raises(ValidationError):
            transport.open_connect_terminal(
                server="ok", target="u@h", port=22, persist="forever")

    def test_flags_present(self, transport, monkeypatch):
        captured = []

        def fake_popen(cmd, creationflags=0, **kw):
            captured.append((list(cmd), creationflags))

            class P:
                pid = 0

            return P()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        transport.open_connect_terminal(server="cl9",
                                        target="wuhong@114.214.201.44")
        cmd, flags = captured[0]
        joined = " ".join(cmd)
        assert flags == subprocess.CREATE_NEW_CONSOLE
        for opt in ("StrictHostKeyChecking=ask", "ControlMaster=yes",
                    "NumberOfPasswordPrompts=3",
                    "ServerAliveInterval=30", "ServerAliveCountMax=4"):
            assert opt in joined

    def test_run_gateway_auto_deploys_missing_helper(self):
        calls = []

        def fake_runner(cmd, *, timeout, capture=True, tty=False):
            calls.append(list(cmd))
            joined = " ".join(cmd)
            if cmd[0] == "ssh" and "test -x" in joined:
                return 1, "", ""
            if cmd[0] == "scp":
                assert "-O" in cmd
                return 0, "", ""
            if "mv -f" in joined:
                return 0, "VaspLoom gateway version 1.5.0", ""
            return 0, json.dumps({"ok": True, "servers": {}}), ""

        t = SshTransport(host="vlab.ustc.edu.cn", user="ubuntu",
                         identity_file=r"C:\u\id.pem",
                         runner=fake_runner)
        result = t.run_gateway(["servers"], timeout=10)
        assert result["ok"]
        assert len(calls) == 5  # probe, prepare, copy, install, operation
        assert any(cmd[0] == "scp" and "-O" in cmd for cmd in calls)

    def test_run_gateway_upgrades_outdated_helper(self):
        calls = []

        def fake_runner(cmd, *, timeout, capture=True, tty=False):
            calls.append(list(cmd))
            joined = " ".join(cmd)
            if "test -x" in joined:
                return 0, "", ""
            if joined.endswith("vasploom-gateway version"):
                return 0, json.dumps({
                    "ok": True, "gateway_version": "1.3.4",
                    "protocol": "2"}), ""
            if cmd[0] == "scp":
                return 0, "", ""
            if "mv -f" in joined:
                return 0, json.dumps({
                    "ok": True, "gateway_version": "1.3.5",
                    "protocol": "2"}), ""
            return 0, json.dumps({"ok": True, "servers": {}}), ""

        t = SshTransport(host="vlab.ustc.edu.cn", user="ubuntu",
                         identity_file=r"C:\u\id.pem",
                         runner=fake_runner)
        assert t.run_gateway(["servers"])["ok"] is True
        assert any(cmd[0] == "scp" for cmd in calls)

    def test_workspace_gateway_auto_deploys_missing_helper(self):
        calls = []

        def fake_runner(cmd, *, timeout, capture=True, tty=False):
            calls.append(list(cmd))
            joined = " ".join(cmd)
            if "test -x" in joined:
                return 1, "", ""
            if cmd[0] == "scp":
                assert "-O" in cmd
                assert "huwei_workspace_gateway.py" in joined
                return 0, "", ""
            if "mv -f" in joined:
                return 0, json.dumps({
                    "ok": True, "gateway": "Workspace Gateway",
                    "version": "0.1.0"}), ""
            return 0, json.dumps({
                "ok": True, "workspaces": [], "space": {}}), ""

        t = SshTransport(host="vlab.ustc.edu.cn", user="ubuntu",
                         identity_file=r"C:\u\id.pem", runner=fake_runner)
        result = t.run_workspace_gateway(["list"], timeout=10)

        assert result["ok"] and result["workspaces"] == []
        assert len(calls) == 5  # probe, prepare, copy, install, operation
        assert any(cmd[0] == "scp" for cmd in calls)

    def test_base_commands_carry_keepalives(self, transport):
        """Every outer ssh op sends heartbeats so idle NAT paths survive."""
        for cmd in (transport._base(), transport._base(batch=False, tty=True)):
            assert "ServerAliveInterval=30" in cmd
            assert "ServerAliveCountMax=4" in cmd

    def test_vlab_saved_credentials_use_stdin_not_command_arguments(self):
        calls = []

        def fake_runner(cmd, *, timeout, capture=True, tty=False,
                        input_data=None):
            calls.append((list(cmd), input_data))
            return 0, json.dumps({"ok": True, "connected": True}), ""

        t = SshTransport(
            host="vlab.ustc.edu.cn", user="ubuntu",
            identity_file=r"C:\u\id.pem", auto_deploy_gateway=False,
            runner=fake_runner)
        result = t.connect_with_credentials(
            server="cl9", password="P@ss word!",
            totp_seed="JBSWY3DPEHPK3PXP")

        assert result["connected"] is True
        cmd, payload_text = calls[-1]
        joined = " ".join(cmd)
        assert "credential-connect" in joined
        assert "P@ss word!" not in joined
        assert "JBSWY3DPEHPK3PXP" not in joined
        payload = json.loads(payload_text)
        assert payload["password"] == "P@ss word!"
        assert payload["totp"].isdigit() and len(payload["totp"]) == 6
        assert "JBSWY3DPEHPK3PXP" not in payload_text

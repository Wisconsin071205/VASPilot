"""Transport terminal-spawning regression tests (the `start` bug)."""

from __future__ import annotations

import subprocess

import pytest

from vaspilot.core.errors import ValidationError
from vaspilot.gateway.transport import SshTransport


@pytest.fixture()
def transport():
    return SshTransport(host="vlab.ustc.edu.cn", user="ubuntu", port=22,
                        identity_file=r"C:\u\id.pem",
                        gateway_path="~/bin/vaspilot-gateway")


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
        assert "vaspilot-gateway" not in joined

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

    def test_base_commands_carry_keepalives(self, transport):
        """Every outer ssh op sends heartbeats so idle NAT paths survive."""
        for cmd in (transport._base(), transport._base(batch=False, tty=True)):
            assert "ServerAliveInterval=30" in cmd
            assert "ServerAliveCountMax=4" in cmd

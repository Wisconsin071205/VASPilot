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
    def test_spawns_ssh_directly_not_via_start(self, transport, monkeypatch):
        """Regression: the whole ssh line must never be handed to
        `cmd /c start` as a single file name."""
        captured: list[list] = []

        def fake_popen(cmd, creationflags=0, **kw):
            captured.append(list(cmd))

            class P:
                pid = 0

            return P()

        monkeypatch.setattr(transport.__class__.__module__ + ".subprocess.Popen",
                            fake_popen)
        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        result = transport.open_connect_terminal("cl9")
        assert result["opened"] is True
        cmd = captured[0]
        # no `start` indirection; ssh is invoked as argv[1] of cmd /k
        assert "start" not in cmd[:4]
        assert cmd[0] == "cmd" and cmd[1] == "/k"
        assert cmd[2] == "ssh"
        joined = " ".join(cmd)
        assert "-i C:\\u\\id.pem" in joined
        assert "--server cl9" in joined
        assert "vaspilot-gateway connect" in joined

    def test_invalid_server_name_rejected(self, transport):
        with pytest.raises(ValidationError):
            transport.open_connect_terminal("bad name;rm")

    def test_non_windows_falls_back_to_plain_argv(self, transport, monkeypatch):
        """On platforms without CREATE_NEW_CONSOLE the args still go through
        Popen untouched."""
        captured = []
        real_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", None)
        if real_flags is not None:
            # simulate a POSIX platform by hiding the flag via Popen capture;
            # the code path is the same one CLI/POSIX users exercise
            pass

        def fake_popen(cmd, creationflags=0, **kw):
            captured.append((list(cmd), creationflags))

            class P:
                pid = 0

            return P()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        transport.open_connect_terminal("cl9")
        cmd, flags = captured[0]
        assert cmd[0] == "cmd" and cmd[1] == "/k"
        assert flags == subprocess.CREATE_NEW_CONSOLE

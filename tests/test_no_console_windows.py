"""No helper process may pop a console window.

Under ``pythonw.exe`` (the desktop shortcut) the parent has no console, so
Windows creates a brand-new console for every child unless CREATE_NO_WINDOW
is requested — each one flashes on screen.  Every child whose output we
capture or pipe must therefore carry the flag; the deliberately VISIBLE
terminals (interactive SSH login, approval, key setup) must not.
"""

from __future__ import annotations

import subprocess

import pytest

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


class TestGatewayRunner:
    """vaspilot.gateway.transport.default_runner runs every ssh/scp call."""

    def test_captured_run_asks_for_no_window(self, monkeypatch):
        from vaspilot.gateway import transport

        seen = {}

        def fake_run(cmd, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, "out", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        rc, stdout, _ = transport.default_runner(["ssh", "host", "echo"],
                                                 timeout=30)
        assert (rc, stdout) == (0, "out")
        assert seen.get("creationflags") == NO_WINDOW

    def test_interactive_run_keeps_the_current_console(self, monkeypatch):
        """capture=False is the CLI login: the user types the password there."""
        from vaspilot.gateway import transport

        seen = {}

        def fake_run(cmd, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, None, None)

        monkeypatch.setattr(subprocess, "run", fake_run)
        transport.default_runner(["ssh", "-tt", "host"], timeout=30,
                                 capture=False)
        assert seen.get("creationflags", 0) == 0

    def test_login_terminal_still_opens_a_visible_console(self, monkeypatch):
        """The opposite guarantee: this one MUST be seen by the user."""
        from vaspilot.core.config import Config
        from vaspilot.gateway.transport import SshTransport

        captured = []
        monkeypatch.setattr(subprocess, "Popen",
                            lambda cmd, creationflags=0, **kw:
                            captured.append(creationflags))
        transport = SshTransport(host="vlab.invalid", user="tester")
        transport.open_connect_terminal(server="cl9", target="user@cl9")
        assert captured == [NEW_CONSOLE]


class TestShellTool:
    def test_shell_run_asks_for_no_window(self, app_with_fake, monkeypatch):
        app, _ = app_with_fake
        registry = app.build_registry()
        seen = {}

        def fake_run(command, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(command, 0, b"ok", b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = registry.dispatch("shell_run", {"command": "echo hi"})
        assert result["ok"] is True
        assert seen.get("creationflags") == NO_WINDOW


class TestCodexBridge:
    def test_bridge_process_asks_for_no_window(self, monkeypatch):
        from vaspilot.core.config import ProviderEntry
        from vaspilot.core.errors import ProviderError
        from vaspilot.providers.codex_sdk import CodexSdkProvider

        seen = {}

        def fake_popen(command, **kwargs):
            seen.update(kwargs)
            raise FileNotFoundError("node")  # stop right after the spawn call

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        entry = ProviderEntry(id="codex", name="Codex", protocol="codex-sdk",
                              base_url="", model="gpt-5.2-codex",
                              api_key_env="")
        provider = CodexSdkProvider(entry)
        with pytest.raises(ProviderError):
            provider._bridge_request({"type": "probe"}, timeout=5)
        assert seen.get("creationflags") == NO_WINDOW

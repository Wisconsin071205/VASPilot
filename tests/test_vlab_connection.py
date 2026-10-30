"""The Vlab lamp checks SSH auth, and only an explicit action opens a terminal."""

from __future__ import annotations

import base64
from datetime import datetime
import subprocess

import pytest

from rsci.cli.main import App
from rsci.core.config import Config
from rsci.core.errors import RemoteError, ValidationError
from rsci.ui import vlab
from rsci.ui.server import UiHandler, build_state


def test_probe_authenticates_with_bounded_silent_ssh(config_home, monkeypatch):
    config = Config(config_home)
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "Welcome\nRSCI_VLAB_CONNECTED\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    result = vlab.check_connection(config)
    assert result["ok"] and result["connected"]
    assert datetime.fromisoformat(result["checked_at"]).utcoffset() is not None
    cmd, options = calls[0]
    assert len(calls) == 1
    assert cmd[-2:] == ["vlab.invalid", "printf 'RSCI_VLAB_CONNECTED\\n'"]
    assert "BatchMode=yes" in cmd and "NumberOfPasswordPrompts=0" in cmd
    assert "StrictHostKeyChecking=yes" in cmd and "UpdateHostKeys=no" in cmd
    assert "ConnectTimeout=8" in cmd and "ConnectionAttempts=1" in cmd
    assert options["timeout"] == 12 and options["stdin"] == subprocess.DEVNULL
    assert options["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert "rsci-gateway" not in " ".join(cmd)


@pytest.mark.parametrize(("rc", "out", "err", "message"), [
    (255, "", "Permission denied (publickey).", "认证失败"),
    (255, "", "Host key verification failed.", "主机密钥"),
    (255, "", "REMOTE HOST IDENTIFICATION HAS CHANGED!", "主机密钥"),
    (255, "", "ssh: Could not resolve hostname vlab.invalid", "无法解析"),
    (255, "", "Connection timed out", "超时"),
    (255, "", "Connection refused", "拒绝"),
    (0, "", "", "未成功"),
    (1, "RSCI_VLAB_CONNECTED\n", "", "未成功"),
])
def test_probe_never_reports_green_for_failed_login(config_home, monkeypatch, rc, out, err, message):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs:
                        subprocess.CompletedProcess(cmd, rc, out, err))
    result = vlab.check_connection(Config(config_home))
    assert result["ok"] and not result["connected"]
    assert message in result["message"]


@pytest.mark.parametrize(("error", "message"), [
    (subprocess.TimeoutExpired("ssh", 12), "12 秒后超时"),
    (FileNotFoundError("ssh"), "未找到 SSH"),
])
def test_probe_handles_timeout_and_missing_ssh(config_home, monkeypatch, error, message):
    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", run)
    result = vlab.check_connection(Config(config_home))
    assert result["ok"] and not result["connected"]
    assert message in result["message"]


def test_missing_identity_fails_without_launching_ssh(config_home, monkeypatch):
    config = Config(config_home)
    config.set_vlab(identity_file=str(config_home / "missing.pem"))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("must not run SSH"))
    assert "私钥文件不存在" in vlab.check_connection(config)["message"]
    with pytest.raises(ValidationError, match="私钥文件不存在"):
        vlab.open_terminal(config)


def test_probe_uses_effective_identity_override(config_home, tmp_path, monkeypatch):
    identity = tmp_path / "override.pem"
    identity.write_text("FAKE")
    monkeypatch.setenv("RSCI_IDENTITY_FILE", str(identity))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs:
                        (calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "RSCI_VLAB_CONNECTED", "")))
    assert vlab.check_connection(Config(config_home))["connected"]
    assert calls[0][calls[0].index("-i") + 1] == str(identity)


@pytest.mark.skipif(vlab.os.name != "nt", reason="Windows terminal wrapper")
def test_terminal_is_visible_interactive_and_quotes_path(config_home, tmp_path, monkeypatch):
    config = Config(config_home)
    identity = tmp_path / "a ' & $HOME %PATH% (key).pem"
    identity.write_text("FAKE")
    config.set_vlab(identity_file=str(identity))
    calls = []
    monkeypatch.setattr(vlab.shutil, "which", lambda exe: "C:/Windows/" + exe)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: calls.append((cmd, kwargs)))
    result = vlab.open_terminal(config)
    assert result["ok"] and result["opened"]
    cmd, options = calls[0]
    assert cmd[:4] == ["C:/Windows/powershell.exe", "-NoLogo", "-NoProfile", "-NoExit"]
    assert cmd[4] == "-EncodedCommand"
    assert options["creationflags"] == subprocess.CREATE_NEW_CONSOLE
    script = base64.b64decode(cmd[5]).decode("utf-16-le")
    assert script.startswith("& 'C:/Windows/ssh' '-tt'")
    assert "'' & $HOME %PATH% (key).pem'" in script
    assert "'StrictHostKeyChecking=yes'" in script
    assert "'ServerAliveInterval=30'" in script and "'ServerAliveCountMax=4'" in script
    assert "BatchMode=yes" not in script
    assert script.endswith("'--' 'vlab.invalid'")


def test_terminal_start_failure_is_actionable(config_home, monkeypatch):
    monkeypatch.setattr(vlab.shutil, "which", lambda exe: exe)
    def fail(*args, **kwargs):
        raise OSError("cannot start")
    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(RemoteError, match="无法打开 Vlab 终端"):
        vlab.open_terminal(Config(config_home))


def test_save_replaces_transport_without_losing_active_work(config_home):
    app = App(Config(config_home))
    client, engine, registry = app.client(), app.engine(), app.registry()
    old_transport = app.transport()
    client._direct_transports["existing"] = object()
    direct = client._direct_transports["existing"]
    result = vlab.save_connection(app, {"host": "other.example", "port": 2222})
    assert result["saved"]["host"] == "other.example"
    assert app.transport() is not old_transport
    assert app.transport().host == "other.example" and app.transport().port == 2222
    assert app.client() is client and app.engine() is engine and app.registry() is registry
    assert client.transport is app.transport() and engine.client.transport is app.transport()
    assert client._direct_transports["existing"] is direct


@pytest.mark.parametrize("changes", [{"host": "host & calc"}, {"user": "-oProxyCommand=calc"}, {"port": 0}, {"port": 65536}])
def test_invalid_save_does_not_change_settings(config_home, changes):
    app = App(Config(config_home))
    original = app.config.vlab
    with pytest.raises(ValidationError):
        vlab.save_connection(app, changes)
    assert app.config.vlab == original


def test_probe_api_does_not_require_client_or_engine(config_home, monkeypatch):
    app = App(Config(config_home))
    monkeypatch.setattr(app, "client", lambda: pytest.fail("must not build client"))
    monkeypatch.setattr(app, "engine", lambda: pytest.fail("must not build engine"))
    expected = {"ok": True, "connected": False, "message": "offline", "checked_at": "now"}
    monkeypatch.setattr(vlab, "check_connection", lambda config: expected)
    handler = object.__new__(UiHandler)
    handler.state = build_state(app)
    calls = []
    handler._send_json = lambda result, **kwargs: calls.append(result)
    handler._handle_api("vlab.check", {})
    assert calls == [expected]


def test_terminal_api_uses_saved_config(config_home, monkeypatch):
    app = App(Config(config_home))
    seen = []
    monkeypatch.setattr(vlab, "open_terminal", lambda config:
                        (seen.append(config.vlab) or {"ok": True, "opened": True}))
    handler = object.__new__(UiHandler)
    handler.state = build_state(app)
    calls = []
    handler._send_json = lambda result, **kwargs: calls.append(result)
    handler._handle_api("vlab.terminal", {"host": "unsaved.invalid"})
    assert seen[0]["host"] == "vlab.invalid"
    assert calls == [{"ok": True, "opened": True}]

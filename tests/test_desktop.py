"""Desktop shell (`vaspilot desktop`): offline tests with a fake pywebview.

No test here imports the real ``webview`` package; a stub module is injected
into ``sys.modules`` so the suite runs on machines without the desktop extra.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from tests.test_cli import run_cli


class FakeHttpd:
    """Stands in for ThreadingHTTPServer: records the shutdown protocol."""

    def __init__(self):
        self.serving = threading.Event()
        self.shutdown_calls = 0
        self.close_calls = 0

    def serve_forever(self):
        self.serving.set()

    def shutdown(self):
        self.shutdown_calls += 1

    def server_close(self):
        self.close_calls += 1


@pytest.fixture()
def fake_webview(monkeypatch):
    """Inject a stub ``webview`` module; records create_window/start calls."""
    calls = {"windows": [], "start": []}
    module = types.ModuleType("webview")

    def create_window(title, url, **kwargs):
        calls["windows"].append({"title": title, "url": url, **kwargs})
        return object()

    def start(**kwargs):
        calls["start"].append(kwargs)

    module.create_window = create_window
    module.start = start
    monkeypatch.setitem(sys.modules, "webview", module)
    return calls


@pytest.fixture()
def fake_serve(config_home, monkeypatch):
    """Replace vaspilot.ui.serve; writes ui.json exactly like the real one."""
    record = {"calls": [], "httpd": FakeHttpd(),
              "url": "http://127.0.0.1:8930/t/tok123"}

    def serve(app, **kwargs):
        record["calls"].append(kwargs)
        (config_home / "ui.json").write_text(json.dumps(
            {"url": "http://127.0.0.1:8930", "token": "tok123",
             "pid": os.getpid()}), encoding="utf-8")
        return record["httpd"], record["url"]

    monkeypatch.setattr("vaspilot.ui.serve", serve)
    return record


@pytest.fixture()
def message_box(monkeypatch):
    shown = []
    monkeypatch.setattr("vaspilot.desktop._message_box",
                        lambda text, **kw: shown.append(text))
    return shown


class TestCli:
    def test_desktop_help_exits_zero(self, monkeypatch):
        code, _, text, _ = run_cli(["desktop", "--help"], monkeypatch)
        assert code == 0
        assert "--port" in text

    def test_desktop_listed_in_top_level_help(self, monkeypatch):
        code, _, text, _ = run_cli(["--help"], monkeypatch)
        assert code == 0
        assert "desktop" in text

    def test_platform_refusal_is_validation_error(self, config_home, monkeypatch):
        from vaspilot.core.errors import ValidationError

        def refuse(app, *, port):
            raise ValidationError("desktop mode needs Windows or macOS")

        monkeypatch.setattr("vaspilot.desktop.main", refuse)
        code, document, _, _ = run_cli(["desktop"], monkeypatch)
        assert code == 5
        assert document["error"]["code"] == "validation_error"

    def test_nonzero_shell_exit_becomes_error(self, config_home, monkeypatch):
        monkeypatch.setattr("vaspilot.desktop.main", lambda app, *, port: 1)
        code, document, _, _ = run_cli(["desktop"], monkeypatch)
        assert code == 1
        assert document["error"]["code"] == "error"


class TestPlatformGuard:
    def test_main_refuses_unsupported_platform(self, config_home):
        from vaspilot.core.errors import ValidationError
        from vaspilot.desktop import main

        with pytest.raises(ValidationError) as caught:
            main(app=None, platform="linux")
        assert "vaspilot ui" in str(caught.value)

    def test_supported_platforms_are_windows_and_macos(self):
        from vaspilot.desktop import GUI_BACKENDS

        assert GUI_BACKENDS == {"win32": "edgechromium", "darwin": "cocoa"}


class TestSelfStart:
    def test_starts_server_opens_window_and_stops_on_close(
            self, config_home, fake_webview, fake_serve):
        from vaspilot.desktop import main, WINDOW_TITLE

        code = main(app=None, platform="win32")

        assert code == 0
        assert fake_serve["calls"] == [
            {"port": 8930, "open_browser": False, "run_forever": False}]
        assert fake_serve["httpd"].serving.wait(2.0)
        assert fake_webview["windows"] == [{
            "title": WINDOW_TITLE, "url": fake_serve["url"],
            "width": 1280, "height": 840, "min_size": (900, 600)}]
        assert fake_webview["start"] == [{"gui": "edgechromium"}]
        assert fake_serve["httpd"].shutdown_calls == 1
        assert fake_serve["httpd"].close_calls == 1
        # we wrote ui.json (pid == ours) so we remove it on exit
        assert not (config_home / "ui.json").exists()

    def test_port_argument_reaches_serve(self, config_home, fake_webview,
                                         fake_serve):
        from vaspilot.desktop import main

        main(app=None, port=8935, platform="win32")
        assert fake_serve["calls"][0]["port"] == 8935

    def test_log_file_records_start(self, config_home, fake_webview,
                                    fake_serve):
        from vaspilot.desktop import main

        main(app=None, platform="win32")
        log = (config_home / "desktop.log").read_text(encoding="utf-8")
        assert "desktop start" in log


class TestPythonnetRuntime:
    def test_defaults_to_netfx(self, config_home, fake_webview, fake_serve,
                               monkeypatch):
        from vaspilot.desktop import main

        monkeypatch.delenv("PYTHONNET_RUNTIME", raising=False)
        main(app=None, platform="win32")
        assert os.environ["PYTHONNET_RUNTIME"] == "netfx"

    def test_keeps_explicit_value(self, config_home, fake_webview,
                                  fake_serve, monkeypatch):
        from vaspilot.desktop import main

        monkeypatch.setenv("PYTHONNET_RUNTIME", "coreclr")
        main(app=None, platform="win32")
        assert os.environ["PYTHONNET_RUNTIME"] == "coreclr"


def _write_ui_json(home: Path, pid: int) -> None:
    (home / "ui.json").write_text(json.dumps(
        {"url": "http://127.0.0.1:8931", "token": "live-token", "pid": pid}),
        encoding="utf-8")


class TestReuse:
    def test_reuses_live_console_without_starting_or_stopping(
            self, config_home, fake_webview, fake_serve, monkeypatch):
        from vaspilot import desktop

        _write_ui_json(config_home, pid=4242)
        monkeypatch.setattr(desktop, "_pid_alive", lambda pid: pid == 4242)
        monkeypatch.setattr(desktop, "_healthz_ok",
                            lambda base: base == "http://127.0.0.1:8931")

        code = desktop.main(app=None, platform="win32")

        assert code == 0
        assert fake_serve["calls"] == []
        assert fake_webview["windows"][0]["url"] == \
            "http://127.0.0.1:8931/t/live-token"
        assert fake_serve["httpd"].shutdown_calls == 0
        # someone else's discovery file is left alone
        assert (config_home / "ui.json").exists()

    def test_dead_pid_falls_back_to_self_start(
            self, config_home, fake_webview, fake_serve, monkeypatch):
        from vaspilot import desktop

        _write_ui_json(config_home, pid=4242)
        monkeypatch.setattr(desktop, "_pid_alive", lambda pid: False)
        monkeypatch.setattr(desktop, "_healthz_ok",
                            lambda base: pytest.fail("must not probe a dead pid"))

        desktop.main(app=None, platform="win32")

        assert len(fake_serve["calls"]) == 1
        assert fake_webview["windows"][0]["url"] == fake_serve["url"]

    def test_unhealthy_console_falls_back_to_self_start(
            self, config_home, fake_webview, fake_serve, monkeypatch):
        from vaspilot import desktop

        _write_ui_json(config_home, pid=4242)
        monkeypatch.setattr(desktop, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(desktop, "_healthz_ok", lambda base: False)

        desktop.main(app=None, platform="win32")
        assert len(fake_serve["calls"]) == 1

    def test_malformed_ui_json_is_ignored(self, config_home, fake_webview,
                                          fake_serve):
        from vaspilot import desktop

        (config_home / "ui.json").write_text("{not json", encoding="utf-8")
        desktop.main(app=None, platform="win32")
        assert len(fake_serve["calls"]) == 1

    def test_read_running_instance_ignores_own_pid(self, config_home,
                                                   monkeypatch):
        from vaspilot import desktop

        _write_ui_json(config_home, pid=os.getpid())
        monkeypatch.setattr(desktop, "_healthz_ok", lambda base: True)
        assert desktop.read_running_instance(config_home) is None


class TestProbes:
    def test_pid_alive_for_self_and_dead_for_bogus(self):
        from vaspilot.desktop import _pid_alive

        assert _pid_alive(os.getpid()) is True
        assert _pid_alive(0) is False
        assert _pid_alive(-1) is False

    def test_healthz_ok_false_when_nothing_listens(self):
        from vaspilot.desktop import _healthz_ok

        # port 9 (discard) is never served on a workstation
        assert _healthz_ok("http://127.0.0.1:9") is False


class TestFallbacks:
    def test_missing_webview_opens_browser_and_keeps_serving(
            self, config_home, fake_serve, message_box, monkeypatch):
        from vaspilot import desktop

        monkeypatch.setitem(sys.modules, "webview", None)  # import -> ImportError
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

        code = desktop.main(app=None, platform="win32")

        assert code == 0
        assert opened == [fake_serve["url"]]
        assert len(message_box) == 1
        assert "pip install -e .[desktop]" in message_box[0]
        # the server we own is only stopped after the serving thread ends
        assert fake_serve["httpd"].shutdown_calls == 1

    def test_all_ports_busy_reports_and_exits_one(
            self, config_home, fake_webview, message_box, monkeypatch):
        from vaspilot import desktop

        def busy(app, **kwargs):
            raise OSError(10048, "address already in use")

        monkeypatch.setattr("vaspilot.ui.serve", busy)
        code = desktop.main(app=None, platform="win32")
        assert code == 1
        assert len(message_box) == 1
        assert "8930" in message_box[0] and "8939" in message_box[0]
        assert fake_webview["windows"] == []

    def test_unexpected_exception_is_logged_and_boxed(
            self, config_home, fake_serve, message_box, monkeypatch):
        from vaspilot import desktop

        def boom(*args, **kwargs):
            raise RuntimeError("WebView2 runtime missing")

        broken = types.ModuleType("webview")
        broken.create_window = boom
        broken.start = lambda **kwargs: None
        monkeypatch.setitem(sys.modules, "webview", broken)

        code = desktop.main(app=None, platform="win32")

        assert code == 1
        assert "WebView2 runtime missing" in message_box[0]
        log = (config_home / "desktop.log").read_text(encoding="utf-8")
        assert "RuntimeError: WebView2 runtime missing" in log
        assert fake_serve["httpd"].shutdown_calls == 1


class TestMacos:
    def test_window_uses_the_cocoa_backend(self, config_home, fake_webview,
                                           fake_serve):
        from vaspilot.desktop import main, WINDOW_TITLE

        code = main(app=None, platform="darwin")

        assert code == 0
        assert fake_webview["start"] == [{"gui": "cocoa"}]
        assert fake_webview["windows"][0]["title"] == WINDOW_TITLE
        assert fake_serve["httpd"].shutdown_calls == 1

    def test_pythonnet_runtime_is_not_touched(self, config_home, fake_webview,
                                              fake_serve, monkeypatch):
        from vaspilot.desktop import main

        monkeypatch.delenv("PYTHONNET_RUNTIME", raising=False)
        main(app=None, platform="darwin")
        assert "PYTHONNET_RUNTIME" not in os.environ

    def test_message_box_uses_osascript_with_argv(self, monkeypatch):
        """Text and title travel as argv, so quotes and newlines need no
        escaping — the reason this is not an -e string interpolation."""
        from vaspilot import desktop

        calls = []
        monkeypatch.setattr(subprocess, "run",
                            lambda cmd, **kw: calls.append(cmd) or
                            subprocess.CompletedProcess(cmd, 0))
        desktop._message_box('he said "hi"\nand left', title="T",
                             platform="darwin")

        assert calls[0][:2] == ["osascript", "-e"]
        assert "display dialog (item 1 of argv)" in calls[0][2]
        assert calls[0][3:] == ['he said "hi"\nand left', "T"]

    def test_message_box_survives_missing_osascript(self, monkeypatch, capsys):
        from vaspilot import desktop

        def missing(cmd, **kwargs):
            raise FileNotFoundError("osascript")

        monkeypatch.setattr(subprocess, "run", missing)
        desktop._message_box("boom", platform="darwin")
        assert "boom" in capsys.readouterr().err

    def test_browser_fallback_reports_the_extra(self, config_home, fake_serve,
                                                message_box, monkeypatch):
        from vaspilot import desktop

        monkeypatch.setitem(sys.modules, "webview", None)
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

        assert desktop.main(app=None, platform="darwin") == 0
        assert opened == [fake_serve["url"]]
        assert "pip install -e .[desktop]" in message_box[0]


class TestPidProbeOnPosix:
    def test_permission_error_counts_as_alive(self, monkeypatch):
        """A console owned by another user is running, not gone."""
        from vaspilot import desktop

        if os.name == "nt":
            pytest.skip("POSIX branch")
        monkeypatch.setattr(os, "kill", lambda pid, sig:
                            (_ for _ in ()).throw(PermissionError()))
        assert desktop._pid_alive(4242) is True

"""Desktop shell for the local web console (``vaspilot desktop``).

The console (``vaspilot.ui``) stays untouched; this module only decides which
server to show (a running one or one it starts itself), opens a native window
on the token URL, and stops the server it owns when the window closes.
pywebview is an optional extra and is imported lazily.

Two platforms are supported, each with its own system web view and its own
way of showing a message when there is no console to print to:

  win32   WebView2 via pythonnet (.NET Framework)   MessageBoxW
  darwin  WKWebView via pyobjc (Cocoa)              osascript display dialog
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
import threading
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from ..core.errors import ValidationError

WINDOW_TITLE = "远端控制智能体"
WINDOW_SIZE = (1280, 840)
WINDOW_MIN_SIZE = (900, 600)
DEFAULT_PORT = 8930
HEALTH_TIMEOUT = 1.5

# platform -> pywebview renderer.  Keys double as the support list.
GUI_BACKENDS = {"win32": "edgechromium", "darwin": "cocoa"}
DIALOG_TIMEOUT = 120


def ui_home() -> Path:
    """Same rule as ui.serve(): VASPILOT_HOME or ~/.vaspilot."""
    return Path(os.environ.get("VASPILOT_HOME") or (Path.home() / ".vaspilot"))


def _open_log(home: Path):
    """Append-mode log; a windowed launch (pythonw, or an .app bundle) has no
    console, so stdout/stderr (None there) are pointed at the same file."""
    home.mkdir(parents=True, exist_ok=True)
    log = open(home / "desktop.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log
    return log


def _log(log, message: str) -> None:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.write(f"{stamp} {message}\n")


def _message_box(text: str, *, title: str = WINDOW_TITLE,
                 platform: str | None = None) -> None:
    """Modal notice; the only UI we have when the launcher has no console."""
    target = platform or sys.platform
    if target == "win32":
        import ctypes
        mb_iconinformation = 0x40
        ctypes.windll.user32.MessageBoxW(None, text, title, mb_iconinformation)
        return
    if target == "darwin":
        # argv carries the strings so neither quotes nor newlines need escaping
        script = ("on run argv\n"
                  "display dialog (item 1 of argv) with title (item 2 of argv)"
                  ' buttons {"OK"} default button "OK" with icon note\n'
                  "end run")
        try:
            subprocess.run(["osascript", "-e", script, text, title],
                           capture_output=True, timeout=DIALOG_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            print(f"{title}: {text}", file=sys.stderr)
        return
    print(f"{title}: {text}", file=sys.stderr)  # pragma: no cover


def _import_webview(platform: str | None = None):
    """Lazy import; on Windows the runtime hint must be set BEFORE pythonnet
    loads (its default coreclr probe fails without a runtimeconfig)."""
    if (platform or sys.platform) == "win32":
        os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")
    import webview  # noqa: WPS433 - optional extra
    return webview


def _pid_alive(pid: int) -> bool:
    """True if a process with this id is still running.

    On Windows ``os.kill(pid, 0)`` would TERMINATE the process, so the check
    goes through OpenProcess/GetExitCodeProcess instead.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited_information,
                                      False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, just owned by someone else
    return True


def _healthz_ok(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/healthz",
                                    timeout=HEALTH_TIMEOUT) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def read_running_instance(home: Path) -> str | None:
    """Token URL of a live console described by ``home/ui.json``, else None.

    A file is trusted only if it names another process that is still alive
    AND answers /healthz; anything else means "no instance" (the file is left
    in place — it is not ours to delete).
    """
    try:
        data = json.loads((home / "ui.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    base, token, pid = data.get("url"), data.get("token"), data.get("pid")
    if not (isinstance(base, str) and isinstance(token, str)
            and isinstance(pid, int)):
        return None
    if pid == os.getpid() or not _pid_alive(pid):
        return None
    if not _healthz_ok(base):
        return None
    return f"{base}/t/{token}"


def _remove_own_ui_json(home: Path) -> None:
    path = home / "ui.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if data.get("pid") == os.getpid():
        try:
            path.unlink()
        except OSError:
            pass


def main(app, *, port: int = DEFAULT_PORT, platform: str | None = None) -> int:
    """Open the console in its own window; returns a process exit code."""
    target = platform or sys.platform
    if target not in GUI_BACKENDS:
        raise ValidationError(
            "desktop mode needs Windows or macOS "
            f"(this is {target}); use `vaspilot ui` instead")
    home = ui_home()
    log = _open_log(home)
    _log(log, f"desktop start pid={os.getpid()} port={port} platform={target}")
    try:
        return _run(app, port=port, home=home, log=log, platform=target)
    except Exception as exc:  # noqa: BLE001 - surface it: there is no console
        _log(log, traceback.format_exc())
        _message_box(f"启动失败：{type(exc).__name__}: {exc}\n\n"
                     f"详见 {home / 'desktop.log'}", platform=target)
        return 1
    finally:
        _log(log, "desktop exit")
        log.close()


def _run(app, *, port: int, home: Path, log, platform: str) -> int:
    httpd = None
    thread = None
    url = read_running_instance(home)
    if url is not None:
        _log(log, f"reusing running console {url.rsplit('/t/', 1)[0]}")
    else:
        from .. import ui
        try:
            httpd, url = ui.serve(app, port=port, open_browser=False,
                                  run_forever=False)
        except OSError as exc:
            _log(log, f"bind failed: {exc}")
            _message_box(f"端口 {port}–{port + 9} 均不可用，无法启动控制台。\n"
                         f"请关闭占用这些端口的程序后重试。\n\n{exc}",
                         platform=platform)
            return 1
        thread = threading.Thread(target=httpd.serve_forever, daemon=True,
                                  name="vaspilot-ui")
        thread.start()
        _log(log, f"serving {url}")
    try:
        try:
            webview = _import_webview(platform)
        except ImportError as exc:
            _log(log, f"pywebview unavailable ({exc}); falling back to browser")
            _message_box("未安装桌面窗口组件，将改用浏览器打开控制台。\n\n"
                         "安装桌面组件：pip install -e .[desktop]",
                         platform=platform)
            webbrowser.open(url)
            if thread is not None:
                thread.join()  # keep the console we own alive (Ctrl-C stops it)
            return 0
        webview.create_window(WINDOW_TITLE, url,
                              width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
                              min_size=WINDOW_MIN_SIZE)
        webview.start(gui=GUI_BACKENDS[platform])
        return 0
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
            _remove_own_ui_json(home)
            _log(log, "server stopped")

"""Desktop shell for the local web console (``vaspilot desktop``).

The console (``vaspilot.ui``) stays untouched; this module only decides which
server to show (a running one or one it starts itself), opens a pywebview /
WebView2 window on the token URL, and stops the server it owns when the
window closes.  pywebview is an optional extra and is imported lazily.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
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


def ui_home() -> Path:
    """Same rule as ui.serve(): VASPILOT_HOME or ~/.vaspilot."""
    return Path(os.environ.get("VASPILOT_HOME") or (Path.home() / ".vaspilot"))


def _open_log(home: Path):
    """Append-mode log; under pythonw there is no console, so stdout/stderr
    (None there) are pointed at the same file."""
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


def _message_box(text: str, *, title: str = WINDOW_TITLE) -> None:
    """Modal notice; the only UI we have when pythonw has no console."""
    if os.name == "nt":
        import ctypes
        mb_iconinformation = 0x40
        ctypes.windll.user32.MessageBoxW(None, text, title, mb_iconinformation)
    else:  # pragma: no cover - not reachable behind the platform guard
        print(f"{title}: {text}", file=sys.stderr)


def _import_webview():
    """Lazy import; the runtime hint must be set BEFORE pythonnet loads."""
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
    except OSError:
        return False
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
    if (platform or sys.platform) != "win32":
        raise ValidationError("desktop mode is Windows-only")
    home = ui_home()
    log = _open_log(home)
    _log(log, f"desktop start pid={os.getpid()} port={port}")
    try:
        return _run(app, port=port, home=home, log=log)
    except Exception as exc:  # noqa: BLE001 - surface it: no console under pythonw
        _log(log, traceback.format_exc())
        _message_box(f"启动失败：{type(exc).__name__}: {exc}\n\n"
                     f"详见 {home / 'desktop.log'}")
        return 1
    finally:
        _log(log, "desktop exit")
        log.close()


def _run(app, *, port: int, home: Path, log) -> int:
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
                         f"请关闭占用这些端口的程序后重试。\n\n{exc}")
            return 1
        thread = threading.Thread(target=httpd.serve_forever, daemon=True,
                                  name="vaspilot-ui")
        thread.start()
        _log(log, f"serving {url}")
    try:
        try:
            webview = _import_webview()
        except ImportError as exc:
            _log(log, f"pywebview unavailable ({exc}); falling back to browser")
            _message_box("未安装桌面窗口组件，将改用浏览器打开控制台。\n\n"
                         "安装桌面组件：pip install -e .[desktop]")
            webbrowser.open(url)
            if thread is not None:
                thread.join()  # keep the console we own alive (Ctrl-C stops it)
            return 0
        webview.create_window(WINDOW_TITLE, url,
                              width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
                              min_size=WINDOW_MIN_SIZE)
        webview.start(gui="edgechromium")
        return 0
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
            _remove_own_ui_json(home)
            _log(log, "server stopped")

# 桌面应用端（`vaspilot desktop`）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有本地 Web 控制台包进一个 pywebview/WebView2 独立窗口，双击桌面快捷方式即打开，关窗即停服。

**Architecture:** 新增 `vaspilot.desktop` 包：复用（或自起）`ui.serve()` 得到带令牌 URL，在守护线程里 `serve_forever`，主线程用 pywebview 开一个窗口加载该 URL，窗口关闭后仅在服务由本进程启动时 `shutdown()`。CLI 增加 `desktop` 子命令；`scripts/install-desktop.ps1` 负责重建 `.venv`、安装 `desktop` extra、创建快捷方式。

**Tech Stack:** Python 3.11（CPython 3.11.15，见全局约束）、pywebview 6.x（`edgechromium` 渲染器，`PYTHONNET_RUNTIME=netfx`）、stdlib（`ctypes`、`urllib`、`zlib`、`struct`）、PowerShell 5.1、pytest。

**Spec:** `docs/superpowers/specs/2026-09-19-desktop-app-design.md`

## Global Constraints

- **所有命令在 Windows 上、仓库 `D:\VASP_new` 内执行**（从 VM 经 `ssh win`；VM 只是跳板，不在 VM 上跑任何项目代码）。隧道会抖动：超过约 60 秒的命令（pytest、pip install）用 `schtasks` 分离执行并把输出写到 `%TEMP%` 文件再取回。
- 解释器：`%APPDATA%\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe`；项目 `.venv` 必须基于它（3.14.0a5 上 pythonnet/cffi 不可用；Store 的 `py -3.12` 是死桩）。下文 `PY` 指 `D:\VASP_new\.venv\Scripts\python.exe`。
- pytest 调用固定为：`PY -m pytest -p no:cacheprovider --basetemp=%TEMP%\vp-pytest -q`（避免 Windows 上 `pytest-current` 符号链接的 PermissionError）。
- 核心包保持零第三方依赖：pywebview 只出现在 `[project.optional-dependencies] desktop`，`vaspilot.desktop` 里对 `webview` 一律**懒导入**。
- 测试全部离线，**不得要求安装 pywebview**（向 `sys.modules` 注入假 `webview`）。
- 窗口标题固定 `远端控制智能体`；尺寸 1280×840，最小 900×600；渲染器 `gui="edgechromium"`。
- 端口默认 8930，回退到 8939（由 `ui.serve()` 内部的 `_bind_with_fallback` 完成，本计划不改它）。
- 错误退出码沿用项目约定：0 ok / 1 error / 5 validation；非 Windows 平台 `ValidationError("desktop mode is Windows-only")`。
- `.gitattributes` 规定 `*.py text eol=lf`：从 Linux 侧上传的 `.py` 必须是 LF；PowerShell 脚本必须带 UTF-8 BOM（PS 5.1 才能正确读中文）。
- 不改 `src/vaspilot/ui/server.py`、`static/index.html`（控制台本身零改动）。
- 分支 `feat/desktop-app`（已从 `master` 开出，含规格提交 `d2c39eb`）；不直接在 `master` 上提交；每个 Task 一次提交，提交信息末尾加 `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`。
- 补丁脚本必须**幂等**（用标记检查再插入），因为 ssh 重试可能让同一脚本执行两次。

---

## 文件结构

| 路径 | 动作 | 职责 |
| --- | --- | --- |
| `src/vaspilot/desktop/__init__.py` | 新增 | `main(app, *, port, platform)`、`read_running_instance(home)`、`_pid_alive`、`_healthz_ok`、`_message_box`、`_import_webview`、日志 |
| `src/vaspilot/desktop/assets/icon.ico` | 新增（生成物，提交） | 快捷方式图标 |
| `scripts/make_icon.py` | 新增 | 用 stdlib 生成上面的 `.ico`（PNG-in-ICO，4 个尺寸） |
| `src/vaspilot/cli/main.py` | 修改 | 注册 `desktop` 子命令 + `cmd_desktop` |
| `pyproject.toml` | 修改 | `desktop` extra；`vaspilot.desktop` package-data |
| `tests/test_desktop.py` | 新增 | 规格 §6 的 6 个测试 + 图标存在性 + 端口全忙 |
| `scripts/install-desktop.ps1` | 新增 | 幂等安装：选解释器 → 重建 `.venv` → `pip install -e .[desktop,dev]` → 桌面/开始菜单快捷方式 |
| `README.md` | 修改 | 安装命令去掉 `py -3.12`；"桌面快捷方式已有"改为真实安装步骤；新增"桌面应用"小节 |

---

### Task 1: `.venv` 重建到 CPython 3.11.15，拿到 314 个测试的绿色基线

**Files:**
- 不改任何被跟踪文件（`.venv` 未被跟踪）。

**Interfaces:**
- Produces: 可用的 `D:\VASP_new\.venv\Scripts\python.exe`（3.11.15）与 `pythonw.exe`，已 `pip install -e .[dev]`。

- [ ] **Step 1: 确认解释器存在且是稳定版**

在 Windows 上运行：

```bat
%APPDATA%\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe -c "import sys;print(sys.version);print(sys.version_info.releaselevel)"
```

Expected: 第一行以 `3.11.15` 开头，第二行 `final`。

- [ ] **Step 2: 删旧 `.venv`，用 3.11.15 重建**

```bat
cd /d D:\VASP_new
rmdir /s /q .venv
%APPDATA%\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe -m venv .venv
.venv\Scripts\python.exe -c "import sys;print(sys.executable, sys.version)"
```

Expected: 打印 `D:\VASP_new\.venv\Scripts\python.exe 3.11.15 ...`。

- [ ] **Step 3: 安装项目（可编辑）+ dev 依赖（分离执行）**

写 `%TEMP%\vp-install.cmd`：

```bat
@echo off
cd /d D:\VASP_new
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -e .[dev] > %TEMP%\vp-install.log 2>&1
echo EXIT=%ERRORLEVEL% >> %TEMP%\vp-install.log
```

用 `schtasks /create /tn vp-install /tr "%TEMP%\vp-install.cmd" /sc once /st 00:00 /f` 加 `schtasks /run /tn vp-install` 启动；轮询直到日志末行出现 `EXIT=`；然后 `schtasks /delete /tn vp-install /f`。

Expected: 日志末行 `EXIT=0`；`.venv\Scripts\python.exe -m pip show vaspilot` 显示 `Location: D:\VASP_new\src`（或 editable 指向仓库）。

- [ ] **Step 4: 跑全部测试（分离执行）**

同样方式运行：

```bat
.venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp=%TEMP%\vp-pytest -q > %TEMP%\vp-pytest.log 2>&1
echo EXIT=%ERRORLEVEL% >> %TEMP%\vp-pytest.log
```

Expected: 日志含 `314 passed`，`EXIT=0`。若失败，先修再进入 Task 2（基线必须绿）。

- [ ] **Step 5: 确认工作区仍干净**

```bat
cd /d D:\VASP_new && git status --short
```

Expected: 无输出（`.venv` 在 `.gitignore` 中）。本 Task 无提交。

---

### Task 2: `desktop` extra、包骨架与 CLI 子命令

**Files:**
- Modify: `pyproject.toml`（`[project.optional-dependencies]`、`[tool.setuptools.package-data]`）
- Create: `src/vaspilot/desktop/__init__.py`
- Modify: `src/vaspilot/cli/main.py:130-137`（`ui` 子命令之后）与 `cmd_ui` 之后
- Test: `tests/test_desktop.py`

**Interfaces:**
- Produces: `vaspilot.desktop.main(app, *, port: int = 8930, platform: str | None = None) -> int`；`vaspilot.desktop.DEFAULT_PORT = 8930`、`WINDOW_TITLE = "远端控制智能体"`。
- Produces: CLI `vaspilot desktop [--port N]`，handler `cmd_desktop(app, args)` 返回 `None`；`main()` 返回非 0 时抛 `VaspilotError`（退出码 1）。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_desktop.py`：

```python
"""Desktop shell (`vaspilot desktop`): offline tests with a fake pywebview.

No test here imports the real ``webview`` package; a stub module is injected
into ``sys.modules`` so the suite runs on machines without the desktop extra.
"""

from __future__ import annotations

import pytest

from tests.test_cli import run_cli


class TestCli:
    def test_desktop_help_exits_zero(self, monkeypatch):
        code, _, text, _ = run_cli(["desktop", "--help"], monkeypatch)
        assert code == 0
        assert "--port" in text

    def test_desktop_listed_in_top_level_help(self, monkeypatch):
        code, _, text, _ = run_cli(["--help"], monkeypatch)
        assert code == 0
        assert "desktop" in text

    def test_non_windows_is_validation_error(self, config_home, monkeypatch):
        from vaspilot.core.errors import ValidationError

        def refuse(app, *, port):
            raise ValidationError("desktop mode is Windows-only")

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
    def test_main_refuses_non_windows(self, config_home):
        from vaspilot.core.errors import ValidationError
        from vaspilot.desktop import main

        with pytest.raises(ValidationError):
            main(app=None, platform="linux")
```

- [ ] **Step 2: 运行确认失败**

Run: `PY -m pytest -p no:cacheprovider --basetemp=%TEMP%\vp-pytest -q tests/test_desktop.py`
Expected: FAIL —— `ModuleNotFoundError: No module named 'vaspilot.desktop'`（以及 `desktop --help` 退出码 2）。

- [ ] **Step 3: 写包骨架**

创建 `src/vaspilot/desktop/__init__.py`（LF 换行）：

```python
"""Desktop shell for the local web console (``vaspilot desktop``).

The console (``vaspilot.ui``) stays untouched; this module only decides which
server to show (a running one or one it starts itself), opens a pywebview /
WebView2 window on the token URL, and stops the server it owns when the
window closes.  pywebview is an optional extra and is imported lazily.
"""

from __future__ import annotations

import sys

from ..core.errors import ValidationError

WINDOW_TITLE = "远端控制智能体"
WINDOW_SIZE = (1280, 840)
WINDOW_MIN_SIZE = (900, 600)
DEFAULT_PORT = 8930


def main(app, *, port: int = DEFAULT_PORT, platform: str | None = None) -> int:
    """Open the console in its own window; returns a process exit code."""
    if (platform or sys.platform) != "win32":
        raise ValidationError("desktop mode is Windows-only")
    return 0
```

- [ ] **Step 4: 注册 CLI 子命令**

在 `src/vaspilot/cli/main.py` 的 `ui.set_defaults(handler=cmd_ui)` 之后、`return parser` 之前插入：

```python
    desktop = sub.add_parser(
        "desktop",
        help="web console in its own desktop window (Windows; pip install -e .[desktop])")
    desktop.add_argument("--port", type=int, default=8930,
                         help="first port to try when no console is running")
    desktop.set_defaults(handler=cmd_desktop)
```

在 `cmd_ui` 之后新增：

```python
def cmd_desktop(app, args):
    """Open the local console in a native window; blocks until it closes."""
    from .. import desktop
    code = desktop.main(app, port=args.port)
    if code:
        raise VaspilotError("desktop shell exited with an error",
                            detail={"exit_code": code})
    return None
```

（`VaspilotError` 已在文件顶部导入。注意用 `desktop.main(...)` 属性访问而不是 `from ..desktop import main`，否则测试里对 `vaspilot.desktop.main` 的 monkeypatch 不生效。）

- [ ] **Step 5: pyproject 加 extra 与 package-data**

`pyproject.toml` 的 `[project.optional-dependencies]` 改为：

```toml
[project.optional-dependencies]
dev = ["pytest>=8.0"]
# Windows-only native window over the local web console (`vaspilot desktop`).
desktop = ["pywebview>=6,<7"]
```

`[tool.setuptools.package-data]` 改为：

```toml
[tool.setuptools.package-data]
"vaspilot.gateway" = ["codex_bridge.mjs"]
"vaspilot.desktop" = ["assets/*.ico"]
```

- [ ] **Step 6: 运行测试确认通过**

Run: `PY -m pytest -p no:cacheprovider --basetemp=%TEMP%\vp-pytest -q tests/test_desktop.py tests/test_cli.py`
Expected: 全部 PASS（5 个新测试 + 原 CLI 测试）。

- [ ] **Step 7: 提交**

```bash
git add pyproject.toml src/vaspilot/desktop/__init__.py src/vaspilot/cli/main.py tests/test_desktop.py
git commit -m "feat(desktop): add desktop extra, package skeleton and CLI subcommand

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 自起路径 + 关窗停服 + `PYTHONNET_RUNTIME`

**Files:**
- Modify: `src/vaspilot/desktop/__init__.py`
- Test: `tests/test_desktop.py`

**Interfaces:**
- Consumes: `vaspilot.ui.serve(app, *, host, port, open_browser, run_forever) -> (httpd, url)`；`url` 形如 `http://127.0.0.1:8930/t/<token>`；`serve` 自己把 `{url(base), token, pid}` 写到 `ui.json`。
- Produces: `vaspilot.desktop.ui_home() -> Path`（与 `serve` 相同规则：`VASPILOT_HOME` 或 `~/.vaspilot`）；`_import_webview()`；`_open_log(home)`；测试夹具 `fake_webview`、`fake_serve`（后续 Task 复用）。

- [ ] **Step 1: 写失败的测试（夹具 + 测试 1、4）**

在 `tests/test_desktop.py` 顶部 `import pytest` 之后加：

```python
import json
import os
import sys
import threading
import types
from pathlib import Path
```

在 `class TestCli` 之前加夹具与假对象：

```python
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
```

在文件末尾加：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `PY -m pytest -p no:cacheprovider --basetemp=%TEMP%\vp-pytest -q tests/test_desktop.py`
Expected: `TestSelfStart` 与 `TestPythonnetRuntime` 全部 FAIL（`fake_serve["calls"] == []`、`KeyError: 'PYTHONNET_RUNTIME'`）。

- [ ] **Step 3: 实现自起路径**

把 `src/vaspilot/desktop/__init__.py` 改为：

```python
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
from pathlib import Path

from ..core.errors import ValidationError

WINDOW_TITLE = "远端控制智能体"
WINDOW_SIZE = (1280, 840)
WINDOW_MIN_SIZE = (900, 600)
DEFAULT_PORT = 8930


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


def _import_webview():
    """Lazy import; the runtime hint must be set BEFORE pythonnet loads."""
    os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")
    import webview  # noqa: WPS433 - optional extra
    return webview


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
    finally:
        _log(log, "desktop exit")
        log.close()


def _run(app, *, port: int, home: Path, log) -> int:
    from .. import ui

    httpd, url = ui.serve(app, port=port, open_browser=False,
                          run_forever=False)
    threading.Thread(target=httpd.serve_forever, daemon=True,
                     name="vaspilot-ui").start()
    _log(log, f"serving {url}")
    try:
        webview = _import_webview()
        webview.create_window(WINDOW_TITLE, url,
                              width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
                              min_size=WINDOW_MIN_SIZE)
        webview.start(gui="edgechromium")
        return 0
    finally:
        httpd.shutdown()
        httpd.server_close()
        _remove_own_ui_json(home)
        _log(log, "server stopped")
```

- [ ] **Step 4: 运行确认通过**

Run: `PY -m pytest -p no:cacheprovider --basetemp=%TEMP%\vp-pytest -q tests/test_desktop.py`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/vaspilot/desktop/__init__.py tests/test_desktop.py
git commit -m "feat(desktop): start the console, open the WebView2 window, stop on close

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 复用正在运行的控制台；死 pid 视为无实例

**Files:**
- Modify: `src/vaspilot/desktop/__init__.py`
- Test: `tests/test_desktop.py`

**Interfaces:**
- Produces: `read_running_instance(home: Path) -> str | None`（返回带令牌的完整 URL 或 `None`）；可打桩的 `_pid_alive(pid: int) -> bool` 与 `_healthz_ok(base_url: str) -> bool`。

- [ ] **Step 1: 写失败的测试（测试 2、3）**

在 `tests/test_desktop.py` 末尾加：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `PY -m pytest -p no:cacheprovider --basetemp=%TEMP%\vp-pytest -q tests/test_desktop.py -k "Reuse or Probes"`
Expected: FAIL —— `AttributeError: module 'vaspilot.desktop' has no attribute '_pid_alive'`。

- [ ] **Step 3: 实现复用路径与探针**

在 `src/vaspilot/desktop/__init__.py` 的 `import threading` 之后加 `import urllib.error`、`import urllib.request`；模块常量后加 `HEALTH_TIMEOUT = 1.5`。在 `_remove_own_ui_json` 之前加：

```python
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
```

把 `_run` 改为：

```python
def _run(app, *, port: int, home: Path, log) -> int:
    httpd = None
    url = read_running_instance(home)
    if url is not None:
        _log(log, f"reusing running console {url.rsplit('/t/', 1)[0]}")
    else:
        from .. import ui
        httpd, url = ui.serve(app, port=port, open_browser=False,
                              run_forever=False)
        threading.Thread(target=httpd.serve_forever, daemon=True,
                         name="vaspilot-ui").start()
        _log(log, f"serving {url}")
    try:
        webview = _import_webview()
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
```

- [ ] **Step 4: 运行确认通过**

Run: `PY -m pytest -p no:cacheprovider --basetemp=%TEMP%\vp-pytest -q tests/test_desktop.py`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/vaspilot/desktop/__init__.py tests/test_desktop.py
git commit -m "feat(desktop): reuse a live console from ui.json; treat dead pid as none

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 错误处理：缺 pywebview 退回浏览器、端口全忙、未捕获异常

**Files:**
- Modify: `src/vaspilot/desktop/__init__.py`
- Test: `tests/test_desktop.py`

**Interfaces:**
- Produces: 可打桩的 `_message_box(text: str, *, title: str = WINDOW_TITLE) -> None`（Windows 上 `ctypes.windll.user32.MessageBoxW`，其他平台仅打印到 stderr）。

- [ ] **Step 1: 写失败的测试（测试 5 + 端口 + 异常）**

在 `tests/test_desktop.py` 末尾加：

```python
@pytest.fixture()
def message_box(monkeypatch):
    shown = []
    monkeypatch.setattr("vaspilot.desktop._message_box",
                        lambda text, **kw: shown.append(text))
    return shown


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
```

- [ ] **Step 2: 运行确认失败**

Run: `PY -m pytest -p no:cacheprovider --basetemp=%TEMP%\vp-pytest -q tests/test_desktop.py -k Fallbacks`
Expected: FAIL —— `AttributeError: ... has no attribute '_message_box'`。

- [ ] **Step 3: 实现**

在 `src/vaspilot/desktop/__init__.py` 中加 `import traceback`、`import webbrowser`。在 `_import_webview` 之前加：

```python
def _message_box(text: str, *, title: str = WINDOW_TITLE) -> None:
    """Modal notice; the only UI we have when pythonw has no console."""
    if os.name == "nt":
        import ctypes
        mb_iconinformation = 0x40
        ctypes.windll.user32.MessageBoxW(None, text, title, mb_iconinformation)
    else:  # pragma: no cover - not reachable behind the platform guard
        print(f"{title}: {text}", file=sys.stderr)
```

把 `main` 的 `try` 块改为：

```python
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
```

把 `_run` 改为：

```python
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
```

（真实 `httpd.serve_forever()` 在浏览器退路下会一直阻塞到进程被结束；这是预期行为 —— 与 `vaspilot ui` 一致。`FakeHttpd.serve_forever` 立即返回，所以测试不会挂。）

- [ ] **Step 4: 运行确认通过**

Run: `PY -m pytest -p no:cacheprovider --basetemp=%TEMP%\vp-pytest -q tests/test_desktop.py`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/vaspilot/desktop/__init__.py tests/test_desktop.py
git commit -m "feat(desktop): browser fallback without pywebview; port and crash notices

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 图标生成脚本与图标资源

**Files:**
- Create: `scripts/make_icon.py`
- Create: `src/vaspilot/desktop/assets/icon.ico`（运行脚本生成后提交）
- Test: `tests/test_desktop.py`

**Interfaces:**
- Produces: `src/vaspilot/desktop/assets/icon.ico`，ICO 容器内 4 张 PNG（256、48、32、16）；`scripts/make_icon.py` 幂等（重跑得到字节相同的文件）。

- [ ] **Step 1: 写失败的测试**

在 `tests/test_desktop.py` 末尾加：

```python
class TestIcon:
    def test_icon_is_packaged_and_well_formed(self):
        import struct
        from importlib import resources

        data = (resources.files("vaspilot.desktop") / "assets" / "icon.ico").read_bytes()
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        assert (reserved, kind) == (0, 1)
        assert count >= 4
        sizes = set()
        for index in range(count):
            width, height, _, _, _, _, size, offset = struct.unpack(
                "<BBBBHHII", data[6 + 16 * index: 6 + 16 * (index + 1)])
            sizes.add(width or 256)
            assert data[offset:offset + 8] == b"\x89PNG\r\n\x1a\n"
            assert offset + size <= len(data)
        assert {256, 48, 32, 16} <= sizes
```

- [ ] **Step 2: 运行确认失败**

Run: `PY -m pytest -p no:cacheprovider --basetemp=%TEMP%\vp-pytest -q tests/test_desktop.py -k Icon`
Expected: FAIL —— `FileNotFoundError` 或 `IsADirectoryError`（`assets` 不存在）。

- [ ] **Step 3: 写生成脚本**

创建 `scripts/make_icon.py`（LF）：

```python
"""Generate src/vaspilot/desktop/assets/icon.ico with the standard library.

Deterministic: rerunning produces byte-identical output.  The mark is a
rounded deep-blue tile with a white chevron ("V" for VASP) and a small
accent dot — readable down to 16 px.  Each size is rendered directly with
4x4 supersampling, then stored as a PNG inside one ICO container (PNG-in-ICO
is supported by Windows Vista and later for every size).

Usage:  python scripts/make_icon.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src" / "vaspilot" / "desktop" / "assets" / "icon.ico"
SIZES = (256, 48, 32, 16)
BLUE = (28, 62, 122)      # tile
WHITE = (255, 255, 255)   # chevron
AMBER = (247, 181, 41)    # accent dot
SUPERSAMPLE = 4


def _inside_tile(x: float, y: float) -> bool:
    """Rounded square covering [0.04, 0.96] with corner radius 0.2."""
    left, right, radius = 0.04, 0.96, 0.2
    if not (left <= x <= right and left <= y <= right):
        return False
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, left + radius), right - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def _inside_chevron(x: float, y: float) -> bool:
    """Two strokes from the top corners meeting at the bottom centre."""
    half_width = 0.11
    top, bottom = 0.26, 0.74
    if not (top <= y <= bottom):
        return False
    t = (y - top) / (bottom - top)           # 0 at top, 1 at the apex
    left_centre = 0.28 + t * (0.5 - 0.28)
    right_centre = 0.72 - t * (0.72 - 0.5)
    return abs(x - left_centre) <= half_width or abs(x - right_centre) <= half_width


def _inside_dot(x: float, y: float) -> bool:
    return (x - 0.5) ** 2 + (y - 0.82) ** 2 <= 0.045 ** 2


def _sample(x: float, y: float) -> tuple[int, int, int, int]:
    if not _inside_tile(x, y):
        return (0, 0, 0, 0)
    if _inside_dot(x, y):
        return (*AMBER, 255)
    if _inside_chevron(x, y):
        return (*WHITE, 255)
    return (*BLUE, 255)


def render(size: int) -> bytes:
    """RGBA scanlines (with PNG filter byte 0) for one square image."""
    rows = bytearray()
    steps = SUPERSAMPLE
    for py in range(size):
        rows.append(0)
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(steps):
                for sx in range(steps):
                    x = (px + (sx + 0.5) / steps) / size
                    y = (py + (sy + 0.5) / steps) / size
                    r, g, b, a = _sample(x, y)
                    acc[0] += r * a
                    acc[1] += g * a
                    acc[2] += b * a
                    acc[3] += a
            n = steps * steps
            alpha = acc[3] // n
            if acc[3]:
                rows += bytes((acc[0] // acc[3], acc[1] // acc[3],
                               acc[2] // acc[3], alpha))
            else:
                rows += b"\x00\x00\x00\x00"
    return bytes(rows)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def png(size: int) -> bytes:
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    body = zlib.compress(render(size), 9)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", body) + _chunk(b"IEND", b""))


def ico(images: list[tuple[int, bytes]]) -> bytes:
    directory = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    blobs = bytearray()
    for size, data in images:
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                                 len(data), offset + len(blobs))
        blobs += data
    return bytes(directory + blobs)


def main() -> None:
    data = ico([(size, png(size)) for size in SIZES])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(data)
    print(f"wrote {OUT} ({len(data)} bytes, sizes {SIZES})")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 生成图标并检查幂等**

```bat
cd /d D:\VASP_new
.venv\Scripts\python.exe scripts\make_icon.py
certutil -hashfile src\vaspilot\desktop\assets\icon.ico SHA256 > %TEMP%\icon1.txt
.venv\Scripts\python.exe scripts\make_icon.py
certutil -hashfile src\vaspilot\desktop\assets\icon.ico SHA256 > %TEMP%\icon2.txt
fc %TEMP%\icon1.txt %TEMP%\icon2.txt
```

Expected: 打印 `wrote ...`；`fc` 报告两文件无差异。用资源管理器或 `powershell -c "[System.Drawing.Icon]::new('D:\VASP_new\src\vaspilot\desktop\assets\icon.ico').Size"`（先 `Add-Type -AssemblyName System.Drawing`）确认可解析。

- [ ] **Step 5: 运行测试确认通过**

Run: `PY -m pytest -p no:cacheprovider --basetemp=%TEMP%\vp-pytest -q tests/test_desktop.py`
Expected: 全部 PASS。同时确认可编辑安装能找到资源：`PY -c "from importlib import resources;print((resources.files('vaspilot.desktop')/'assets'/'icon.ico').is_file())"` 打印 `True`。

- [ ] **Step 6: 提交**

```bash
git add scripts/make_icon.py src/vaspilot/desktop/assets/icon.ico tests/test_desktop.py
git commit -m "feat(desktop): stdlib-generated application icon (PNG-in-ICO)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: `install-desktop.ps1` 与 README

**Files:**
- Create: `scripts/install-desktop.ps1`（UTF-8 **带 BOM**）
- Modify: `README.md:25-40`（安装段）、`README.md:97`（快捷方式句）、`## 统一 Web 控制台` 之后新增小节

**Interfaces:**
- Consumes: `.venv\Scripts\pythonw.exe -m vaspilot desktop`（Task 2）；`src\vaspilot\desktop\assets\icon.ico`（Task 6）。
- Produces: 桌面与开始菜单的 `远端控制智能体.lnk`。

- [ ] **Step 1: 写安装脚本**

创建 `scripts/install-desktop.ps1`（文件开头写入 BOM `EF BB BF`；从 Linux 侧生成时用 `printf '\xEF\xBB\xBF' > file && cat body >> file`）：

```powershell
<#
.SYNOPSIS
  Install the desktop shell for the local console and create shortcuts.

.DESCRIPTION
  Idempotent. Picks a stable CPython >= 3.11 (never an alpha/beta and never
  the Microsoft Store stub), rebuilds .venv on it if the current .venv uses a
  different interpreter, installs the project with the desktop extra, verifies
  pywebview imports, and (re)creates the Desktop and Start Menu shortcuts that
  launch `pythonw -m vaspilot desktop` in the repository directory.

.PARAMETER Python
  Explicit interpreter to use instead of the automatic search.
#>
[CmdletBinding()]
param(
    [string]$Python = ""
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Repo '.venv'
$ShortcutName = '远端控制智能体.lnk'
$Icon = Join-Path $Repo 'src\vaspilot\desktop\assets\icon.ico'

function Test-StablePython([string]$Exe) {
    if (-not $Exe -or -not (Test-Path $Exe)) { return $null }
    try {
        $info = & $Exe -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}.{v.micro} {v.releaselevel}')" 2>$null
    } catch { return $null }
    if ($LASTEXITCODE -ne 0 -or -not $info) { return $null }
    $parts = $info.Trim().Split(' ')
    $ver = [version]$parts[0]
    if ($parts[1] -ne 'final') { return $null }
    if ($ver -lt [version]'3.11.0') { return $null }
    return [pscustomobject]@{ Exe = $Exe; Version = $ver }
}

function Find-StablePython {
    $candidates = @()
    if ($Python) { $candidates += $Python }
    $uvRoot = Join-Path $env:APPDATA 'uv\python'
    if (Test-Path $uvRoot) {
        $candidates += Get-ChildItem $uvRoot -Directory |
            Where-Object { $_.Name -like 'cpython-3.*-windows-*' } |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName 'python.exe' }
    }
    foreach ($tag in '3.13', '3.12', '3.11') {
        try {
            $path = & py "-$tag" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $path -and ($path -notmatch 'WindowsApps')) { $candidates += $path.Trim() }
        } catch { }
    }
    foreach ($exe in $candidates) {
        $ok = Test-StablePython $exe
        if ($ok) { return $ok }
    }
    throw "No stable CPython >= 3.11 found. Pass -Python C:\path\to\python.exe"
}

$chosen = Find-StablePython
Write-Host "Interpreter: $($chosen.Exe) ($($chosen.Version))"

$rebuild = $true
$cfg = Join-Path $Venv 'pyvenv.cfg'
if (Test-Path $cfg) {
    $line = (Get-Content $cfg | Where-Object { $_ -match '^version\s*=' }) -replace '^version\s*=\s*', ''
    if ($line -and ([version]$line.Trim()) -eq $chosen.Version) { $rebuild = $false }
}
if ($rebuild) {
    if (Test-Path $Venv) {
        Write-Host "Removing .venv (different interpreter)"
        Remove-Item -Recurse -Force $Venv
    }
    Write-Host "Creating .venv"
    & $chosen.Exe -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
} else {
    Write-Host ".venv already on $($chosen.Version); keeping it"
}

$venvPy = Join-Path $Venv 'Scripts\python.exe'
$venvPyw = Join-Path $Venv 'Scripts\pythonw.exe'
Write-Host "Installing project with desktop + dev extras"
& $venvPy -m pip install --disable-pip-version-check -e "$Repo[desktop,dev]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$env:PYTHONNET_RUNTIME = 'netfx'
& $venvPy -c "import webview; print('pywebview', webview.__version__)"
if ($LASTEXITCODE -ne 0) { throw "pywebview import check failed" }
if (-not (Test-Path $Icon)) { throw "icon missing: $Icon (run scripts\make_icon.py)" }

$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) $ShortcutName),
    (Join-Path ([Environment]::GetFolderPath('Programs')) $ShortcutName)
)
foreach ($lnk in $targets) {
    $sc = $shell.CreateShortcut($lnk)
    $sc.TargetPath = $venvPyw
    $sc.Arguments = '-m vaspilot desktop'
    $sc.WorkingDirectory = $Repo
    $sc.IconLocation = "$Icon,0"
    $sc.Description = 'VASPilot desktop console'
    $sc.WindowStyle = 1
    $sc.Save()
    Write-Host "Shortcut: $lnk"
}
Write-Host "Done. Double-click the shortcut to open the console window."
```

- [ ] **Step 2: 语法检查（不执行安装）**

```bat
powershell -NoProfile -Command "$null = [scriptblock]::Create((Get-Content -Raw D:\VASP_new\scripts\install-desktop.ps1)); 'PARSE-OK'"
powershell -NoProfile -Command "(Get-Content -Encoding Byte -TotalCount 3 D:\VASP_new\scripts\install-desktop.ps1) -join ','"
```

Expected: `PARSE-OK`；第二条打印 `239,187,191`（BOM）。

- [ ] **Step 3: 实际运行安装脚本（分离执行，`/it` 使其在交互会话跑）**

写 `%TEMP%\vp-install-desktop.cmd`：

```bat
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File D:\VASP_new\scripts\install-desktop.ps1 > %TEMP%\vp-install-desktop.log 2>&1
echo EXIT=%ERRORLEVEL% >> %TEMP%\vp-install-desktop.log
```

`schtasks /create /tn vp-install-desktop /tr "%TEMP%\vp-install-desktop.cmd" /sc once /st 00:00 /it /f` → `/run` → 轮询日志 → `/delete`。

Expected: 日志含 `.venv already on 3.11.15; keeping it`、`pywebview 6.`、两行 `Shortcut: ...`、末行 `EXIT=0`。再跑一次，日志相同（幂等）。`dir "%USERPROFILE%\Desktop\远端控制智能体.lnk"` 存在。

- [ ] **Step 4: 更新 README**

(a) 把 `## Install (development)` 代码块改为：

```powershell
# any stable CPython >= 3.11 (the Store `py -3.12` stub does not work here)
python -m venv .venv
.venv\Scripts\python -m pip install -e .[dev]
huwei --help               # 兼容命令: vaspilot --help
.venv\Scripts\python -m pytest         # fully offline
```

(b) 把其余 `py -3.12` 替换为 `.venv\Scripts\python`（`set PYTHONPATH=src && ...` 与 `scripts\install_vlab_gateway.py` 两处）。

(c) 把行 `桌面快捷方式「VASPilot 控制台」或 \`%USERPROFILE%\bin\vaspilot-ui.cmd\` 一键启动。` 替换为：

```markdown
命令行启动 `vaspilot ui`（或 `%USERPROFILE%\bin\vaspilot-ui.cmd`）；桌面窗口形态见下一节。
```

(d) 在 `## VS Code 安全编辑` 之前插入新小节：

```markdown
## 桌面应用（`vaspilot desktop`）

同一个控制台，装进独立窗口（pywebview / WebView2，Windows 专用）：双击桌面快捷方式
「远端控制智能体」即打开，关闭窗口即停止服务；若控制台已在运行（`vaspilot ui`），
只开窗、不重复起服务。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-desktop.ps1
```

脚本幂等：选择稳定的 CPython ≥ 3.11（跳过 Store 桩与 alpha 版），必要时重建 `.venv`，
安装 `desktop` extra（`pip install -e .[desktop]`），在桌面与开始菜单创建快捷方式
（目标 `.venv\Scripts\pythonw.exe -m vaspilot desktop`，无控制台窗口）。
未安装 extra 时 `vaspilot desktop` 会提示并退回浏览器。日志：`~/.vaspilot/desktop.log`。
```

- [ ] **Step 5: 复核 README 无遗漏**

```bat
cd /d D:\VASP_new && findstr /n /c:"py -3.12" README.md & findstr /n /c:"VASPilot 控制台" README.md
```

Expected: 两条都无输出。

- [ ] **Step 6: 跑全部测试**

Run（分离）：`PY -m pytest -p no:cacheprovider --basetemp=%TEMP%\vp-pytest -q`
Expected: 全部通过（314 + 本计划新增约 20 个）。

- [ ] **Step 7: 提交**

```bash
git add scripts/install-desktop.ps1 README.md
git commit -m "feat(desktop): install script (venv + extra + shortcuts); README install/desktop docs

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Windows 手工验收与合并

**Files:**
- 无代码改动；仅 git 合并。

- [ ] **Step 1: 冷启动验收（无控制台运行）**

先确认没有旧服务：`powershell -c "Get-Process pythonw -EA SilentlyContinue | Measure-Object | % Count"` 为 0；`netstat -ano | findstr :8930` 无输出。
用 `schtasks ... /it` 分离执行：`"D:\VASP_new\.venv\Scripts\pythonw.exe" -m vaspilot desktop`（等价于双击快捷方式）。等 8 秒后检查：

- `netstat -ano | findstr :8930` 有 LISTENING；
- `type %USERPROFILE%\.vaspilot\ui.json` 的 `pid` 等于该 `pythonw` 进程；
- `type %USERPROFILE%\.vaspilot\desktop.log` 末尾含 `desktop start` 与 `serving http://127.0.0.1:8930/t/`；
- `powershell -c "(Get-Process pythonw).MainWindowTitle"` 为 `远端控制智能体`。

Expected: 以上全部满足（窗口在用户桌面可见）。

- [ ] **Step 2: 关窗停服**

`powershell -c "(Get-Process pythonw) | % { $_.CloseMainWindow() }"`，等 3 秒：

Expected: `Get-Process pythonw` 无结果；`netstat -ano | findstr :8930` 无输出；`ui.json` 已删除；日志末尾 `server stopped` / `desktop exit`。

- [ ] **Step 3: 复用验收**

分离启动 `vaspilot ui --no-open`（`.venv\Scripts\python.exe -m vaspilot ui --no-open`），等 3 秒；再分离启动 `pythonw -m vaspilot desktop`。

Expected: 日志出现 `reusing running console http://127.0.0.1:8930`；`netstat` 仍只有一个 8930 监听（属于 `python.exe`）。关闭窗口后 `vaspilot ui` 进程仍在、8930 仍监听。最后结束 `vaspilot ui`（`taskkill /pid <pid>`），清理 `ui.json`。

- [ ] **Step 4: 缺 extra 退路验收**

`.venv\Scripts\python.exe -c "import sys,vaspilot.desktop as d; sys.modules['webview']=None; sys.exit(d.main(None))"`（分离 `/it`）：

Expected: 弹出提示框（含 `pip install -e .[desktop]`），默认浏览器打开控制台；进程持续服务直到被结束。结束该进程后清理。

- [ ] **Step 5: 快进合并到 master**

```bash
cd /d D:\VASP_new
git status --short          # 必须为空
git checkout master
git merge --ff-only feat/desktop-app
git log --oneline -1
git checkout feat/desktop-app   # 或留在 master；两者相同
```

Expected: `master` 指向 `feat/desktop-app` 的最新提交，无合并提交。

---

## 自审记录

- **规格覆盖**：§1 目标（Task 3/8）；§2 决策——3.11.15 venv（Task 1）、`netfx`（Task 3）、`desktop` extra（Task 2）；§3 生命周期全部分支（Task 3、4）；§4 每个文件都有对应 Task（`__init__.py` 2–5，`icon.ico`/`make_icon.py` 6，`cli/main.py` 2，`pyproject` 2，`install-desktop.ps1`/README 7，`test_desktop.py` 2–6）；§5 五种情形（Task 5 与 Task 2 的平台守卫）；§6 六个测试 —— 1→`TestSelfStart`、2→`TestReuse` 第一个、3→`TestReuse` 第二个、4→`TestPythonnetRuntime`、5→`TestFallbacks` 第一个、6→`TestCli`；手工验收→Task 8；§7 顺序与 Task 1–8 一致；§8 只要求"不依赖 cwd"（`ui_home()` 与 `importlib.resources` 满足）。
- **占位符**：无 TBD/TODO；每个代码步骤有完整代码。
- **类型一致性**：`main(app, *, port, platform)` 在 Task 2/3/4/5 与测试中一致；`fake_serve["calls"]` 记录 kwargs 且 Task 3 断言的键集合与 `_run` 实际传参一致（`port`、`open_browser`、`run_forever`）；`_pid_alive`/`_healthz_ok`/`_message_box` 的名字在实现与 monkeypatch 中一致；`WINDOW_SIZE`/`WINDOW_MIN_SIZE` 与测试断言的 1280/840/(900, 600) 一致。

# 桌面应用端（`vaspilot desktop`）设计

日期：2026-09-19 · 状态：已批准，待实施 · 方案：A（pywebview 壳）

## 1. 目标与边界

把现有的本地 Web 控制台（`vaspilot ui`）以**独立窗口的桌面应用**形式提供：
双击桌面快捷方式即弹出自己的窗口（有标题栏与图标，没有浏览器地址栏和标签页），
关闭窗口即停止服务。先服务本机使用，架构上预留日后用 PyInstaller 打包分发的可能。

不在本次范围内：系统托盘常驻、自动更新、安装包（.msi/.exe）、多窗口、
对控制台本身（`ui/server.py`、`static/index.html`）的任何功能改动。

## 2. 关键决策与理由

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 窗口技术 | pywebview 6.x（WebView2 / `edgechromium` 渲染器） | 纯 Python，复用控制台 100%，独立进程与生命周期；Windows 11 自带 WebView2 运行时。探针已在 CPython 3.11.15 上验证可开窗（`loaded=true`） |
| .NET 运行时 | 强制 `PYTHONNET_RUNTIME=netfx` | pythonnet 默认 coreclr 在无 runtimeconfig 时失败；.NET Framework 4.8 在 Win11 上必然存在 |
| 解释器 | 项目 `.venv` 从 3.14.0a5 重建为 CPython 3.11.15 | cffi 的编译后端在 3.14 alpha 上加载 DLL 报 `TypeError`，pythonnet 链整体不可用；3.11.15 已在盘上（`%APPDATA%\uv\python\...`），零安装；稳定解释器也是日后 PyInstaller 的前提 |
| 依赖策略 | `desktop` 作为**可选 extra** | 项目核心保持零第三方依赖的既有原则；未安装 extra 时 `desktop` 命令退回浏览器并提示 |
| 被否方案 | Electron（多一套语言、~200MB、分发仍需套 PyInstaller）；Edge `--app=` 模式（非独立进程、关窗停服不可靠）；Tauri（无 Rust） | — |

## 3. 架构与生命周期

```
vaspilot desktop
   │
   ├─ 读 ~/.vaspilot/ui.json ──► pid 存活 且 GET {url}/healthz == 200 ?
   │        │ 是：复用（不起服务，关窗不停服）
   │        ▼ 否：
   ├─ serve(app, open_browser=False, run_forever=False) ──► (httpd, url)
   │     绑定 8930..8939；serve() 自己写 ui.json {url, token, pid}
   ├─ 守护线程 httpd.serve_forever()
   ├─ os.environ.setdefault("PYTHONNET_RUNTIME", "netfx"); import webview
   ├─ create_window("远端控制智能体", url, 1280×840, min 900×600)
   ├─ webview.start(gui="edgechromium")   ← 阻塞到窗口关闭
   └─ 仅当服务是本进程启动的：httpd.shutdown(); server_close(); 删除自己写的 ui.json
```

`url` 即 `serve()` 返回的带令牌地址 `http://127.0.0.1:<port>/t/<token>`；
窗口直接载入它，不经过落地页。

## 4. 组件与文件

| 路径 | 变更 | 职责 |
| --- | --- | --- |
| `src/vaspilot/desktop/__init__.py` | 新增 | `main(argv) -> int`：第 3 节流程 + 第 5 节错误处理；pywebview 懒导入；约 150 行 |
| `src/vaspilot/desktop/assets/icon.ico` | 新增（生成后提交） | 应用与快捷方式图标；由 `scripts/make_icon.py` 用 stdlib（`zlib`+`struct`，PNG-in-ICO）生成一次 |
| `src/vaspilot/cli/main.py` | 修改 | 在 `ui` 旁注册 `desktop` 子命令（参数仅 `--port`，默认 8930）；handler 返回 `None`（不输出 JSON） |
| `pyproject.toml` | 修改 | `[project.optional-dependencies] desktop = ["pywebview>=6,<7"]`；package-data 加入 `vaspilot.desktop = ["assets/*.ico"]` |
| `scripts/install-desktop.ps1` | 新增（UTF-8 BOM） | 幂等安装：定位稳定 Python ≥3.11 → 必要时重建 `.venv` → `pip install -e .[desktop,dev]` → 桌面 + 开始菜单创建 `远端控制智能体.lnk` |
| `README.md` | 修改 | 把"桌面快捷方式已有"的表述改为真实安装命令；安装示例不再写死 `py -3.12` |
| `tests/test_desktop.py` | 新增 | 第 6 节 |

快捷方式规格：目标 `<repo>\.venv\Scripts\pythonw.exe`，参数 `-m vaspilot desktop`，
工作目录 `<repo>`，图标 `<repo>\src\vaspilot\desktop\assets\icon.ico`。用 `pythonw`
是为了不弹控制台窗口。

## 5. 错误处理

| 情形 | 行为 |
| --- | --- |
| `import webview` 失败（未装 extra / 运行时缺失） | `ctypes.windll.user32.MessageBoxW` 提示 `pip install -e .[desktop]`，随后 `webbrowser.open(url)` 退回浏览器；退出码 0（功能可用，仅形态降级） |
| 8930–8939 全部不可绑定 | MessageBox 说明端口冲突；退出码 1 |
| `ui.json` 存在但 pid 已死或 healthz 不通 | 视为无现有实例，走自起路径；不删别人的文件 |
| 任何未捕获异常 | 写入日志后 MessageBox 摘要；退出码 1 |
| 日志 | `pythonw` 无控制台，全部输出追加到 `~/.vaspilot/desktop.log` |

非 Windows 平台：`desktop` 命令直接报 `ValidationError("desktop mode is Windows-only")`，退出码 5。

## 6. 测试

全部离线，且**不要求安装 pywebview**（CI/其他机器也能跑）。

`tests/test_desktop.py`（向 `sys.modules` 注入假 `webview` 模块，`serve` 与 `urllib` 打桩）：
1. 自起路径：`serve` 以 `open_browser=False, run_forever=False` 被调用；`create_window` 收到的 URL 等于 `serve` 返回的令牌 URL；`start` 返回后 `httpd.shutdown` 与 `server_close` 各被调用一次。
2. 复用路径：`ui.json` 指向存活 pid 且 healthz=200 时不调用 `serve`，窗口 URL 为 `{url}/t/{token}`，关窗后不调用任何 shutdown。
3. `ui.json` 指向已死 pid → 走自起路径。
4. `PYTHONNET_RUNTIME` 未设时被设为 `netfx`；已设为其他值时不覆盖。
5. `import webview` 抛 `ImportError` → 调用 `webbrowser.open(url)`，返回 0。
6. CLI：`vaspilot desktop --help` 退出 0；`desktop` 在子命令列表中。

手工验收（Windows）：运行 `scripts/install-desktop.ps1` → 桌面出现图标 → 双击弹窗并能登录/浏览 → 关窗后 `Get-Process pythonw` 无残留、8930 端口释放 → 服务已在运行时双击第二次只开窗不起新服务。

## 7. 实施顺序

1. `.venv` 重建到 3.11.15，`pip install -e .[dev]`，重跑现有 314 个测试确认全绿（基线）。
2. `pyproject` extra + `desktop` 包骨架 + CLI 注册（TDD：先写第 6 节测试 6）。
3. `desktop.main` 自起路径 + 关窗停服（测试 1、4）。
4. 复用路径与死 pid 处理（测试 2、3）。
5. 错误处理与浏览器退路（测试 5）。
6. 图标生成脚本 + 图标提交 + package-data。
7. `install-desktop.ps1` + README 更新。
8. Windows 手工验收；在 `master` 上以快进合并收尾。

## 8. 分发预留（不实施）

`desktop.main` 不依赖当前工作目录（图标经 `importlib.resources` 读取），日后 PyInstaller
`--onedir --noconsole --icon` 只需补一个 spec 文件；`install-desktop.ps1` 的快捷方式
逻辑可改为指向打包产物。

# 远端控制智能体 1.5.0 程序部署说明

本压缩包是“一〇七杯”比赛第 3 项“智能体／可部署程序文件”。提供 Windows x64 可执行程序、Python wheel 安装包和源码。请先完整解压，再启动；不要在压缩软件中直接运行 exe，也不要单独移动 exe（需要同目录的 _internal）。

## Windows 快速启动

1. 使用 Windows 10/11 64 位系统，双击 `Start-rsci.cmd`。此版本自带 Python 运行时，无需安装 Python。
2. 浏览器自动打开本地控制台。若未打开，复制启动窗口中 `rsci UI ready:` 后的完整地址。默认端口 8930，被占用时会顺延；地址含本次会话令牌。
3. 保持启动窗口运行；关闭窗口或按 Ctrl+C 停止服务。`Check-rsci.cmd` 可检查命令行入口。
4. 在“配置”中添加模型服务（协议、API 地址、模型名、API Key），在服务器页面添加实际服务器连接信息。首次启动没有预置个人账号和任务数据。

命令行用法：`windows\rsci\rsci.exe --help`；智能体入口 `agent`，控制台入口 `ui`。配置默认保存在当前用户目录 `.rsci`，可用 `RSCI_HOME` 指定独立目录。

## 运行所需外部条件

- 本地网页与命令行可离线启动；调用远程模型需要可访问的模型 API 和自己的有效密钥。支持 OpenAI Chat 兼容接口、Responses 接口等，详见源码 README。
- 连接 HPC 需要系统提供 `ssh`、`scp`（Windows OpenSSH 客户端）、有效集群账号以及可访问的直连或 Vlab 路由。请按控制台指引手工完成认证或由本人保存凭据。
- 实际计算需要远端 Slurm/PBS 环境以及合法安装的 VASP 和 POTCAR；本包不包含 VASP、赝势、SSH 私钥、密码、TOTP 种子或 API Key。
- 远端网关要求 Python 3；启用自动部署时由应用部署。源码 `scripts/install_vlab_gateway.py` 提供手动安装方式。
- 可选 Codex 提供方需要另行安装 Node.js 和相应 Codex 环境。可选材料科学库不包含在免安装版内；需要这些库时使用 Python 安装方式并安装相应 extras。
- 可选 VS Code Bridge 提供源码，需安装 Node.js 后在 `source/apps/huwei-agent-vscode-bridge` 执行 `npm ci`、`npm run package:dist`，再运行 `install-windows.ps1`。完整工作区还需要 Vlab 的 rclone/FUSE，参见 `source/gateway/huwei-workspace-gateway/README.md`。

## Python 安装方式

需要 Python 3.11 或更新版本；本次在 Windows Python 3.12 验证。打开终端进入解压目录：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --no-index --no-deps python-package/rsci-1.5.0-py3-none-any.whl
.venv\Scripts\rsci.exe --help
.venv\Scripts\rsci.exe ui
```

wheel 的核心运行时无第三方 Python 依赖。非 Windows 系统可使用 `.venv/bin/python` 和 `.venv/bin/rsci`，但 Windows DPAPI 凭据保存功能仅支持 Windows；本次未验证其他操作系统。

## 源码与校验

`source/` 保留当前源码、测试和配方，可执行 `python -m pip install -e ./source[dev]` 后在 source 目录运行 `python -m pytest`。重建提交包还需 `build`、`setuptools`、`wheel`、`pyinstaller`，运行 `python scripts/build_submission.py`。

`SHA256SUMS.json` 记录包内各文件 SHA-256（清单自身除外）；外部 `.zip.sha256` 用于核对整个压缩包。测试记录随提交文件提供。离线启动测试不等同于真实模型调用或 HPC 作业运行证明。

本包只对应第 3 项。正式提交时，应与设计文档、演示视频、作品简介等材料合并成一个总压缩文件，并按预通知要求使用队长学号、手机号、赛道和队伍类别命名。

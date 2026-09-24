# rsci 晶织：VASP 服务端计算控制智能体

> 英文项目名、Python 包名和主命令统一为 `rsci`；中文产品名为
> 「晶织：VASP 服务端计算控制智能体」。`huwei` 保留为兼容命令。

CLI-first, multi-model VASP/HPC agent: after the user completes SSH
authentication by hand, it performs **auditable** file transfer, job
submission, status monitoring and VASP workflows across
`Windows → USTC Vlab → multiple remote HPC servers` by default; each server
may instead use direct SSH when reachable from the workstation. Named tools
are path-confined and validated. The two explicit shell tools (`shell_run`,
`remote_run`) trade that confinement for autonomy and are governed by the
audit log; they are refused to `analysis_only` providers. See
[Shell access](#shell-access-explicit-operator-policy).

```
本地 CLI / 智能体层       可选路由                     HPC 适配层
────────────────         ─────────────                ─────────────
模型调用 (3 协议)   SSH   Vlab 网关（默认）       mux  Slurm/PBS 查询/提交/取消
工作流规划 + 审批   ───>   或目标服务器直连       ───> 文件与目录操作
审计 / 多服务器监测       固定操作 + 根路径校验       VASP 验证/进度/解析
                          可选凭据仅本机 DPAPI 加密
```

## 作业自动分析：Slurm 集群运行证明

在作业页点击“自动分析”，可查看“集群运行证明”并下载 Markdown 证明材料或 JSON 原始数据，用于整理作品在本科生算力平台上的实际运行材料：

- 调取 `squeue` 排队状态、等待原因或运行节点，`sacct` 提交/开始/结束时间、运行时长、分区、CPU 数、退出码及各作业步骤的内存统计，`sstat` 运行中内存采样。
- 保留服务器、集群、作业 ID、计算目录、UTC 采集时间、查询命令、原始响应及日志摘录；日志每个文件最多末尾 240 行、65536 字符，总计最多 8 个文件。
- 等待时长由实际开始时间减提交时间计算；尚未开始时不将预计开始时间当作实际等待结果。申请内存与 MaxRSS 分开呈现，后者为步骤内单任务最大常驻内存，不是整个作业总内存。
- 集群未启用记账、作业记录过期、非 Slurm 调度器或日志不可读时显示缺失原因，并继续整理其他可用数据。每次点击产生一次快照，不补造历史采样；通用日志来自所选目录，共用目录需核对归属。

远端网关需为 1.3.5 或更新版本；启用自动部署时按已有连接流程更新，否则需使用现有安装脚本更新网关。导出数据用于材料整理，不代表平台认证。字段口径参见 [Slurm sacct](https://slurm.schedmd.com/sacct.html) 与 [sstat](https://slurm.schedmd.com/sstat.html) 官方文档。

## Install (development)

比赛第 3 项程序包的启动、部署及重建方法见
[`docs/submission-deployment.md`](docs/submission-deployment.md)。
执行 `python scripts/build_submission.py` 生成 Windows 免安装程序、wheel 和源码压缩包；
执行 `python scripts/verify_submission.py` 验证解压后的 exe 与独立环境安装的 wheel。

```powershell
# CPython >= 3.11
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,desktop]"
.venv\Scripts\rsci.exe --help
.venv\Scripts\python -m pytest
```

材料自动化栈按需安装，不强迫轻量网关携带科学计算依赖：

```powershell
py -3.12 -m pip install -e .[materials]   # pymatgen + custodian + atomate2
py -3.12 -m pip install -e .[dispatch]    # DPDispatcher
py -3.12 -m pip install -e .[provenance]  # AiiDA
```

当前稳定执行后端仍是受限 gateway；这些可选包的职责边界、部署前提和迁移顺序见
[`spec/automation-stack.md`](spec/automation-stack.md)。

也可直接使用仓库启动脚本，确保运行当前源码：

```powershell
scripts\rsci.cmd server list          # wrapper that pins PYTHONPATH=src
# or
$env:PYTHONPATH = "src"
.venv\Scripts\python -m rsci --help
```

默认本机配置目录为 `~/.rsci`，环境变量使用 `RSCI_` 前缀，新部署的远端网关为
`~/bin/rsci-gateway`。从早期 VaspLoom 配置迁移时，密钥前缀使用
`VaspLoom`；从 VASPilot 配置迁移时使用 `VASPilot`：

```powershell
python -m rsci.core.migration --source <原配置目录> --target <新配置目录> `
    --source-entropy-prefix VaspLoom
```

该迁移会保留原目录，在内存中解密并重新加密凭据；目标目录已有配置时拒绝覆盖。
迁移会保留原目录，在内存中解密并重新加密凭据，保留已有服务器地址、路径和其他参数；
目标目录已有配置时拒绝覆盖。迁移后刷新控制台即可重新读取配置。

## First-run setup

```powershell
# 1) Vlab identity (PEM from the portal) — path only, never the key body
setx RSCI_IDENTITY_FILE "C:\path\to\vlab.pem"

# 2) for Vlab-routed servers, the first operation automatically checks and,
#    by default, deploys the gateway helper onto Vlab; the one-shot manual
#    command is still available (see below), then:
rsci server add cl9 --target user@cl9.ustc.edu.cn --root /public/home/you/rsci --scheduler slurm --route vlab
# 目标可从本机直接访问时，可改用：--route direct
rsci server connect cl9        # 无加密凭据时在可见终端输入密码 + TOTP

# Web 控制台中可为 Vlab 中转或直连服务器保存密码与 TOTP 种子；
# 之后点击“连接”会自动认证。每 3 天在服务器卡片点击“确认凭据”即可继续使用。
# CLI 也可安全交互录入（密码不会进入命令历史）：
rsci server credentials-save cl9 --confirm-days 3

# 3) model providers (keys stay in environment variables)
rsci agent provider add --id ds --name DeepSeek --protocol openai-chat-compatible `
    --base-url https://api.deepseek.com/v1 --model deepseek-chat --api-key-env DEEPSEEK_API_KEY
rsci agent provider probe ds   # capability probe; failures degrade to analysis_only
```

Gateway deployment onto Vlab (runs `scp` + `ssh`, CRLF→LF + py_compile
validation, atomic replace). This is normally automatic when a Vlab operation
finds `~/bin/rsci-gateway` missing; the command below is useful for a
manual install or repair:

```powershell
.venv\Scripts\python scripts\install_vlab_gateway.py --identity-file C:\path\to\vlab.pem
```

The gateway is a small, restricted helper stored in the Vlab user's
`~/bin/rsci-gateway`. It is the only component that talks from Vlab to the
registered HPC targets; the model never receives an arbitrary remote shell.
Automatic deployment first checks for an executable helper, then uploads the
local helper to a random `/tmp` staging name, byte-compiles it on Vlab, and
atomically replaces the destination only after validation. Disable it in
“配置 → 连接设置 → 网关缺失时自动部署” when deployments must be managed
manually. The separate Workspace Gateway (rclone/FUSE) remains a manual,
explicit install because it has additional Vlab-side prerequisites.

## Command map

| Group | Commands |
| --- | --- |
| `server` | `list add edit remove connect disconnect status set-default doctor` |
| `remote` | `pwd list read tail find stat du upload download mkdir copy move trash trash-list restore purge` |
| `job` | `list recent submit cancel progress diagnose` |
| `workflow` | `prepare validate preview approve approve-submit run resume status` |
| `monitor` | `snapshot watch` |
| `agent` | `provider list/add/remove/probe/set-default`, `chat --provider`, `run --provider --goal` |
| `ui` | `rsci ui` — 统一 Web 控制台 |
| `desktop` | `rsci desktop` — 控制台的独立桌面窗口（Windows / macOS） |

Every command prints one stable JSON document and uses documented exit codes:
`0` ok · `1` error · `2` usage · `3 auth_required` · `4 approval` · `5 validation`.
`auth_required` never opens a hidden prompt. Direct-route credentials are used
only when explicitly saved and within the confirmation window; otherwise you
re-authenticate visibly via `server connect`.

## 统一 Web 控制台（`rsci ui`）

本地零依赖 Web UI（默认 `http://127.0.0.1:8930`，浏览器自动打开）：

- **对话** — 多 Provider 智能体聊天，流式输出、持久记忆、会话分支、上下文压缩、
  安全 Markdown 渲染和工具调用卡片（可展开参数/结果），与 CLI/MCP 共用同一注册表
  和权限语义；
- **总览** — 全服务器快照（连接、调度器、活动作业、状态）+ 60 秒自动刷新；
- **文件** — 远端目录浏览与小型文本查看（POTCAR/大二进制拒绝）；
- **作业** — 活动/历史作业 + VASP 科学进度卡；每个作业可读取受限日志尾部并运行
  内置规则诊断（调度器状态与科学收敛始终分列）；
- **计划中心** — 普通计算任务从「对话」发起；智能体准备好本地输入后，生成
  单阶段权威计划卡片并暂停等待人工审批。计划中心集中查看文件 SHA-256、有序步骤、
  风险、执行尝试和失败恢复；高级用户仍可手动创建计划。

安全模型：仅绑定 127.0.0.1；每次启动生成随机会话令牌，所有 `/api/*` 请求
必须携带。可选的服务器密码/TOTP 与模型 API Key 会用 Windows DPAPI
（CurrentUser）加密后保存在 `~/.rsci`；Vlab 中转仅临时发送密码和当期验证码，
TOTP 种子不离开本机。未保存凭据时，交互式登录通过「连接」按钮在**独立的可见
系统终端**中完成；审批短语由人在页面输入、服务端按 CLI 相同规则校验。
命令行启动 `rsci ui`（仓库内包装器 `scripts\rsci.cmd ui` 固定使用本仓库代码）；
独立窗口的桌面形态见下一节。

## 桌面应用（`rsci desktop`）

同一个控制台，装进独立窗口：双击图标即打开，关闭窗口即停止服务；若控制台已在运行
（`rsci ui` 或另一个窗口），只开窗、不重复起服务，关窗也不会停掉别人的服务。
窗口用系统自带的 web view，不额外装浏览器内核：

| 平台 | 渲染器 | 安装脚本 | 入口 |
| --- | --- | --- | --- |
| Windows | WebView2（`edgechromium`，经 pythonnet/.NET Framework） | `scripts\install-desktop.ps1` | 桌面与开始菜单的 `晶织：RSCI` 快捷方式 |
| macOS | WKWebView（`cocoa`，经 pyobjc） | `scripts/install-desktop.sh` | `~/Applications/晶织-RSCI.app` |

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-desktop.ps1
```

macOS：

```bash
scripts/install-desktop.sh            # 或 scripts/install-desktop.sh /path/to/python3
```

两个脚本都幂等：选择稳定的 CPython >= 3.11（跳过 alpha 版；Windows 上还跳过 Store 桩），
必要时重建 `.venv`，安装 `desktop` extra（`pip install -e .[desktop]`；pywebview 会按平台
自动带上 pythonnet 或 pyobjc），生成图标，最后创建入口。Windows 的快捷方式指向
`pythonw.exe` 以免弹控制台；macOS 生成一个最小的 `.app`，其 launcher 进入仓库目录后执行
`python -m rsci desktop`。

未安装 extra 时 `rsci desktop` 会弹提示并退回浏览器；Linux 直接拒绝并让你用
`rsci ui`。日志：`~/.rsci/desktop.log`。

## VS Code 安全编辑（单一最新版 VS Code）

Vlab 中转和直连服务器都可以在编辑窗口保存登录密码和 Google Authenticator 的
TOTP 种子；两者使用 Windows DPAPI（CurrentUser）加密后写入
`~/.rsci/settings.json`。直连时仅在本机 SSH 子进程中短暂解密；Vlab
中转只经已验证的 SSH 通道临时发送密码和当期六位码，TOTP 种子不会离开本机，
Vlab 不会落盘保存凭据。密码和种子绝不进入审计日志或聊天记录。默认每 3 天要求一次「确认仍保留凭据」，确认前
自动连接会暂停；未保存凭据时仍通过「连接」按钮在**独立可见系统终端**中手工输入。
审批短语由人在页面输入、服务端按 CLI 相同规则校验。桌面快捷方式「晶织 RSCI 控制台」
或 `%USERPROFILE%\bin\rsci-ui.cmd` 一键启动。

## VS Code 安全编辑（单一最新版 VS Code）

仓库附带“晶织 RSCI VS Code Bridge”扩展。它经本地控制台按需访问
`Windows → Vlab → HPC`，不通过 Remote-SSH 直接连接计算服务器，因此不会在
CentOS 7 等老集群安装 VS Code Server。先运行
`apps\huwei-agent-vscode-bridge\install-windows.ps1` 安装仓库内的 VSIX，随后在
控制台“文件”页右击文件或文件夹，选择“在 VS Code 中安全编辑”或“以虚拟目录打开”。
详细说明见 [docs/VSCode安全编辑.md](docs/VSCode安全编辑.md)。

需要终端、语言服务或完整目录时，可启用 Vlab 完整工作区模式：VS Code 只连接
Vlab，rclone 在 Vlab 映射一个明确选定的计算目录。安装、空间限制、写入租约和
断线恢复说明见 [docs/VSCode远端工作区使用说明.md](docs/VSCode远端工作区使用说明.md)。

## 单阶段 VASP 作业提交与审批

```
rsci workflow prepare --from-dir CASE --server cl9 --remote-dir /public/home/you/runs/case-1
rsci workflow preview <plan_id>        # plan_hash, files+SHA-256, steps, risk
rsci workflow approve  <plan_id>       # YOU type "approve <plan_id>" locally
rsci workflow run      <plan_id> --approval-ref <ref>
rsci workflow resume   <plan_id> --approval-ref <ref>   # new attempt, same plan
```

当前执行引擎每次只提交一个阶段：输入目录中的 `INCAR`、`KPOINTS`、`POSCAR` 和
`POTCAR`（或远端已预置的 POTCAR）会先经过本地/远端门禁，然后生成
`mkdir → upload → validate → submit → monitor → progress → download → parse`
的不可变计划。项目模板不会放入假的 Fe POSCAR；必须提供真实结构。

`--skip-potcar` 只代表“不上传本地 POTCAR”，不代表 VASP 不需要 POTCAR，也不会由
工作流自动组装远端库文件。远端运行目录必须在校验前已有正确的已组装 POTCAR。

The approval is an HMAC token bound to **server + plan_hash + files_hash**,
valid for a window, consumable once (replay rejected; resume only by the run
instance that consumed it). Any change to servers, files, script or steps
changes `plan_hash` and voids every approval. Uploads re-verify local SHA-256
against the approved plan. `cancel` needs a double-matched job id; deletions
go to a recoverable trash; `purge` needs a double-matched trash id.

In the Web UI, the equivalent flow is: describe the calculation in 「对话」 →
review the generated plan card → type `approve <plan_id>` → 「审批并执行」.
The model does not manually upload or submit that same standard local-input run;
after approval, the immutable workflow engine owns the execution and the chat
and 「计划中心」 show its persisted status.

Scheduler `COMPLETED` is **never** reported as scientific convergence — the
run state records `scheduler_state` and `scientific_converged` separately,
and a scheduler-finished-but-unconverged run ends as `needs_review`.

## Shell access (explicit operator policy)

Two tools give the model a real shell. This is intentional (added in v1.1.0
with the agent-autonomy suite) and it is the one place where the confinement
described above does **not** apply:

| Tool | Runs on | Confinement |
| --- | --- | --- |
| `shell_run` | the local Windows machine | none — `subprocess.run(..., shell=True)` |
| `remote_run` | the HPC login node, via the gateway | none — no `remote_root` check, no `realpath` guard; keeps a per-session working directory |

Consequences worth stating plainly:

- `remote_run` bypasses the `remote_root` boundary and the symlink-escape
  check that every other remote operation enforces;
- `remote_run` can call `sbatch`/`qsub` directly, so it also bypasses the
  human confirmation that `job_submit` shows in the web console;
- the only gate on either tool is provider capability: they are `write`-kind,
  so a provider degraded to `analysis_only` is refused. A fully probed
  provider needs no approval token.

The control plane is therefore the **audit log**, not interception: every
command, working directory, exit code and output is recorded on both sides.
Set `RSCI_MCP_MODE=analysis_only` to remove both tools from an MCP
session, and review `~/.rsci/audit/` if you need to know what ran.

## Model providers

| Protocol | Backends | Notes |
| --- | --- | --- |
| `openai-chat-compatible` | DeepSeek, GLM, Ollama, LM Studio | streaming + function tools |
| `openai-responses` | OpenAI Responses API | streaming events + function tools |
| `codex-sdk` | Codex (official SDK if installed, else `codex exec --json`) | minimal Node bridge (`codex_bridge.mjs`), read-only sandbox; tool execution stays in Python |

Provider config stores only `id/name/protocol/base_url/model/api_key_env` —
the key is read from the named environment variable at call time and never
persisted. Before remote-control mode every provider runs a capability probe
(reachable, streaming, tool calling, structured JSON); any failure degrades it
to `analysis_only`, which the registry enforces by refusing write/scheduler
tools.

## Codex plugin

`C:\Users\weikx\plugins\rsci-remote-control` is a thin stdio relay to
`python -m rsci.mcp` — the same registry the CLI uses. It adds
`open_remote_login` and `open_approval_terminal` (visible terminals) and never
carries credentials or approval references through MCP.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `RSCI_HOME` | config directory (default `~/.rsci`) |
| `RSCI_IDENTITY_FILE` | Vlab PEM path override |
| `<api_key_env>` per provider | e.g. `DEEPSEEK_API_KEY`, `OPENAI_API_KEY` |
| `RSCI_POTCAR_LIBRARY` | authorized local POTCAR library (metadata-only access) |
| `RSCI_MCP_MODE` | `analysis_only` forces read-only MCP sessions |

## Security invariants

- exactly two shell tools (`shell_run`, `remote_run`), both audit-only by
  explicit operator policy and both refused to `analysis_only` providers;
  every other tool maps to a fixed command shape and never accepts a
  caller-supplied shell string
本机直连凭据的字段、周期确认和失败语义见
[spec/local-credential-requirements.md](spec/local-credential-requirements.md)。
- strict validation of server names, paths (lexical + `realpath`), job ids,
  trash ids and local project roots
- SSH host-key changes fail closed; `known_hosts` is never auto-modified
- Per-server passwords and TOTP seeds are optional DPAPI-encrypted CurrentUser
  blobs. Direct mode uses a short-lived local `SSH_ASKPASS`; Vlab mode sends
  only the password and current code over verified SSH stdin. Periodic
  retention confirmation (default 3 days) gates automatic use
- SHA-256 recorded for every upload/download/analysis artifact
- POTCAR readable only as metadata (TITEL/ENMAX/size/SHA-256) from an
  authorized library
- append-only, secret-sanitizing audit log under `~/.rsci/audit/`
- gateway staging area confined to `/tmp/rsci-<hex>` with hash checks

## Repository layout

```
src/rsci/   core/ (config, validation, audit, hashing)
                gateway/ (SSH transport, client, rsci_gateway.py for Vlab)
                hpc/ (Slurm/PBS adapters, VASP science, job scripts)
                providers/ (3 protocols + codex_bridge.mjs)
                tools/ (the named tool registry shared by CLI/MCP/agent)
                workflow/ (single-stage plan, approval tokens, engine)
                agents/ (tool-calling runtime)
                cli/ mcp/
tests/          unit, provider-contract (mock HTTP), gateway integration
                (real gateway script over a fake HPC), CLI, MCP
recipes/ spec/  design drafts plus the single-stage execution specification
```

## Known limitations

- Vlab-routed servers require the Vlab-side gateway to be deployed manually
  the first time (`scripts/install_vlab_gateway.py`), unless the Web/CLI
  setting “网关缺失时自动部署” is enabled (the default);
- direct-route servers need Python 3 and the helper is installed under the
  target user's `~/.cache/rsci/` on first use. Native Windows OpenSSH
  does not support `ControlMaster`/`ControlPath`; direct operations use a
  short-lived `SSH_ASKPASS` helper when an encrypted password/TOTP seed is
  present, or an SSH key/agent. Credentials are withheld after the periodic
  confirmation deadline until the user confirms retention.
  Vlab-routed targets can use the same local encrypted vault; the reusable
  TOTP seed remains local and the Vlab gateway does not persist credentials.
  Vlab complete-workspace mode remains available only for Vlab-routed
  servers;
- `server doctor` probes DNS/reachability/identity/gateway/scheduler but
  cannot fix anything by design;
- Codex-provider probing without the SDK falls back to the `codex` CLI and an
  offline probe never certifies execution capabilities;
- transfer (`remote copy` across servers) is capped at 8 GiB;
- real-server acceptance (submission on cl9/cl12/minus/chemistry) still
  requires your explicit approval of the target directories.

# VASPilot

CLI-first, multi-model VASP/HPC agent: after the user completes SSH
authentication by hand, it performs **restricted, auditable** file transfer,
job submission, status monitoring and VASP workflows across
`Windows → USTC Vlab → multiple remote HPC servers`. No model is ever given
an arbitrary remote shell — every action is a named, validated tool.

```
本地 CLI / 智能体层        Vlab 网关层                HPC 适配层
────────────────          ─────────────              ─────────────
模型调用 (3 协议)    SSH   服务器目录(非敏感)     mux  Slurm/PBS 查询/提交/取消
工作流规划 + 审批    ───>  每服务器 SSH 复用       ───> 文件与目录操作
审计 / 多服务器监测        转发固定操作                   VASP 验证/进度/解析
                          不存密码/TOTP/私钥
```

## Install (development)

```powershell
py -3.12 -m pip install -e .[dev]
vaspilot --help            # 或: py -3.12 -m vaspilot --help
py -3.12 -m pytest         # 134 tests, fully offline
```

**Note:** an older `vaspilot` 0.3.0 may be installed globally on this machine
(from `D:\VASP`). Until you deliberately replace it, invoke this repository
explicitly:

```powershell
scripts\vaspilot.cmd server list          # wrapper that pins PYTHONPATH=src
# or
set PYTHONPATH=src && py -3.12 -m vaspilot --help
```

## First-run setup

```powershell
# 1) Vlab identity (PEM from the portal) — path only, never the key body
setx VASPILOT_IDENTITY_FILE "C:\path\to\vlab.pem"

# 2) deploy the gateway helper onto Vlab (one command, see below), then:
vaspilot server add cl9 --target user@cl9.ustc.edu.cn --root /public/home/you/vaspilot --scheduler slurm
vaspilot server connect cl9        # YOU type the password + TOTP here

# 3) model providers (keys stay in environment variables)
vaspilot agent provider add --id ds --name DeepSeek --protocol openai-chat-compatible `
    --base-url https://api.deepseek.com/v1 --model deepseek-chat --api-key-env DEEPSEEK_API_KEY
vaspilot agent provider probe ds   # capability probe; failures degrade to analysis_only
```

Gateway deployment onto Vlab (runs `scp` + `ssh`, CRLF→LF + py_compile
validation, atomic replace):

```powershell
py -3.12 scripts\install_vlab_gateway.py --identity-file C:\path\to\vlab.pem
```

## Command map

| Group | Commands |
| --- | --- |
| `server` | `list add edit remove connect disconnect status set-default doctor` |
| `remote` | `pwd list read tail find stat du upload download mkdir copy move trash trash-list restore purge` |
| `job` | `list recent submit cancel progress diagnose` |
| `workflow` | `prepare validate preview approve approve-submit run resume status` |
| `monitor` | `snapshot watch` |
| `agent` | `provider list/add/remove/probe/set-default`, `chat --provider`, `run --provider --goal` |

Every command prints one stable JSON document and uses documented exit codes:
`0` ok · `1` error · `2` usage · `3 auth_required` · `4 approval` · `5 validation`.
`auth_required` is never auto-filled — the CLI opens no hidden prompt; you
re-authenticate visibly via `server connect`.

## Approval model (immutable plan, one approval, unattended inside the plan)

```
vaspilot workflow prepare --from-dir CASE --server cl9 --remote-dir /public/home/you/runs/case-1
vaspilot workflow preview <plan_id>        # plan_hash, files+SHA-256, DAG, risk
vaspilot workflow approve  <plan_id>       # YOU type "approve <plan_id>" locally
vaspilot workflow run      <plan_id> --approval-ref <ref>
vaspilot workflow resume   <plan_id> --approval-ref <ref>   # new attempt, same plan
```

The approval is an HMAC token bound to **server + plan_hash + files_hash**,
valid for a window, consumable once (replay rejected; resume only by the run
instance that consumed it). Any change to servers, files, script or steps
changes `plan_hash` and voids every approval. Uploads re-verify local SHA-256
against the approved plan. `cancel` needs a double-matched job id; deletions
go to a recoverable trash; `purge` needs a double-matched trash id.

Scheduler `COMPLETED` is **never** reported as scientific convergence — the
run state records `scheduler_state` and `scientific_converged` separately,
and a scheduler-finished-but-unconverged run ends as `needs_review`.

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

`C:\Users\weikx\plugins\vaspilot-remote-control` is a thin stdio relay to
`python -m vaspilot.mcp` — the same registry the CLI uses. It adds
`open_remote_login` and `open_approval_terminal` (visible terminals) and never
carries credentials or approval references through MCP.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `VASPILOT_HOME` | config directory (default `~/.vaspilot`) |
| `VASPILOT_IDENTITY_FILE` | Vlab PEM path override |
| `<api_key_env>` per provider | e.g. `DEEPSEEK_API_KEY`, `OPENAI_API_KEY` |
| `VASPILOT_POTCAR_LIBRARY` | authorized local POTCAR library (metadata-only access) |
| `VASPILOT_MCP_MODE` | `analysis_only` forces read-only MCP sessions |

## Security invariants

- no shell tool; no `bash -c`/`sh -c`/PowerShell string concatenation anywhere
- strict validation of server names, paths (lexical + `realpath`), job ids,
  trash ids and local project roots
- SSH host-key changes fail closed; `known_hosts` is never auto-modified
- SHA-256 recorded for every upload/download/analysis artifact
- POTCAR readable only as metadata (TITEL/ENMAX/size/SHA-256) from an
  authorized library
- append-only, secret-sanitizing audit log under `~/.vaspilot/audit/`
- gateway staging area confined to `/tmp/vaspilot-<hex>` with hash checks

## Repository layout

```
src/vaspilot/   core/ (config, validation, audit, hashing)
                gateway/ (SSH transport, client, vaspilot_gateway.py for Vlab)
                hpc/ (Slurm/PBS adapters, VASP science, job scripts)
                providers/ (3 protocols + codex_bridge.mjs)
                tools/ (the named tool registry shared by CLI/MCP/agent)
                workflow/ (plan, approval tokens, engine)
                agents/ (tool-calling runtime)
                cli/ mcp/
tests/          unit, provider-contract (mock HTTP), gateway integration
                (real gateway script over a fake HPC), CLI, MCP
recipes/ spec/  migrated workflow recipes and specifications
```

## Known limitations

- the Vlab-side gateway must be deployed manually the first time
  (`scripts/install_vlab_gateway.py`);
- `server doctor` probes DNS/reachability/identity/gateway/scheduler but
  cannot fix anything by design;
- Codex-provider probing without the SDK falls back to the `codex` CLI and an
  offline probe never certifies execution capabilities;
- transfer (`remote copy` across servers) is capped at 8 GiB;
- real-server acceptance (submission on cl9/cl12/minus/chemistry) still
  requires your explicit approval of the target directories.

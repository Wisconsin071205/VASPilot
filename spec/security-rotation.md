# 认证信息轮换检查清单

> 背景：conversation-export.html 曾包含服务器认证信息。已将其加入 .gitignore 并禁止入库。
> **凡出现在任何导出、日志或聊天记录中的凭据，一律视为已泄露，必须轮换。**

## 必须轮换的凭据（按优先级）

| # | 凭据 | 轮换方法 | 验证 |
|---|---|---|---|
| 1 | cl9/cl10 服务器登录密码 | SSH 登录后 `passwd` | 新旧密码均不再出现在任何文件中 |
| 2 | TOTP 种子（六位验证码） | 在认证源（如统一身份认证/企业微信/Google Authenticator）重新生成绑定，**旧 seed 作废** | 旧 OTP 无法再登录 |
| 3 | Vlab PEM 私钥 | Vlab 平台「SSH 密钥管理」重新生成并下载，替换 `~/.ssh/vlab-identity.pem` | 旧公钥从 Vlab 授权列表删除 |
| 4 | DeepSeek/OpenAI API Key | 服务商控制台吊销并新建 | 旧 key 调用立即 401 |
| 5 | 本机 Windows 凭据中的 API Key | UI 设置中更新为新 key（secure_store 会覆盖加密条目） | — |
| 6 | 本地账号密码（vaspilot 历史加密用） | 若账号密码曾在导出中出现，更换密码并重新加密历史 | — |

## 轮换后清理

1. 删除或重新生成脱敏版 `conversation-export.html`（用 `scripts/export-chat-html.py` 配合
   `scripts/training-record.py` 的脱敏字段，或手工替换主机名/账号/路径/密钥片段）。
2. 检查 `~/.cache/vasp-remote-agent/audit.jsonl` 与 `scripts/*.log` 是否含敏感片段；有则截断或脱敏。
3. `git log --all --full-history` 确认敏感内容从未进入仓库；若曾提交，需要 `git filter-repo`
   重写历史（不可只做新提交）。
4. 更新 `references/setup.md` 中的示例 PEM 文件名（仅作示例，不写真实路径）。

## 脱敏规则（用于重新生成可分享导出）

- 主机名/账号：`user@192.0.2.1` → `user@host`
- 绝对路径：`/home/user/...` → `/home/user/...`；`D:\VASP project\...` → `D:\project\...`
- PEM 文件名/内容片段：全部移除
- 密码、OTP、TOTP seed、API Key：全部移除
- Slurm 作业号、记账账户：脱敏为 `<job-id>`、`<account>`
- POTCAR 全文：只保留 TITEL 行
- 保留：命令结构、INCAR 参数、错误签名、状态流转、审批行为（这些是评测素材）

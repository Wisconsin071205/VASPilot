# WebDAV 配置与对话同步

RSCI 可以把本机工作数据同步到任意支持 WebDAV 的服务，默认面向中国科大
`pan.ustc.edu.cn` 的 Seafile WebDAV 端点 `https://pan.ustc.edu.cn/seafdav`。
同步是可选的：不配置时应用完全离线工作。

## 配置入口

打开控制台 **配置 → WebDAV 同步**，填写：

| 字段 | 说明 |
| --- | --- |
| WebDAV 地址 | 例如 `https://pan.ustc.edu.cn/seafdav`。省略 `https://` 会自动补全。 |
| 用户名 | Seafile 登录名；科大账号通常是 `学号@auth.local` 形式，以门户显示为准。 |
| 密码 | 登录密码。保存后只显示“已保存”，页面不会回填明文。 |
| 远端目录 | 默认为 `RSCI-sync`。Seafile 的 `/seafdav` 根目录是资料库列表，通常需要填写已有资料库下的子目录，例如 `webdav/RSCI-sync`；可先点“测试连接”查看根目录下现有资料库。 |
| 启用 WebDAV 同步 | 总开关。关闭后仍可手动测试和同步，但自动同步不会运行。 |
| 自动同步 | 由桌面/网页控制台后台按间隔执行。 |
| 自动同步间隔 | 1–1440 分钟，默认 15 分钟。 |

点击 **保存 WebDAV 设置** 后，可用 **测试连接** 验证认证和远端目录；
**立即同步** 执行一次双向同步；**查看冲突副本** 列出本机保留的冲突文件；
**清除密码** 删除本机保存的 WebDAV 密码。

密码使用本机密钥加密：

- Windows：DPAPI `CurrentUser`，与本机用户绑定；
- Linux/macOS：`~/.rsci/secret.key`（权限 0600）派生的 AES-256-GCM 密钥；
- 也可用环境变量 `RSCI_WEBDAV_PASSWORD` 临时覆盖保存值，适合 CI 或共享机器。
  `RSCI_WEBDAV_URL`、`RSCI_WEBDAV_USERNAME` 不会被自动读取，避免意外切换目标。

POSIX 上加密需要 `cryptography` 包：

```bash
python -m pip install -e ".[sync]"
```

## 同步内容

上传到远端 `RSCI-sync/` 的内容：

- `settings.json`：非敏感设置（模型服务条目、Vlab 主机/用户/端口、联网搜索
  提供商、智能体提交策略等）；
- `servers.json`：服务器连接目录（目标、端口、根目录、调度器，不含密码/TOTP）；
- `projects.json`：项目索引；
- `chat/*.json`：对话日志（用户/助手消息、归档、项目绑定、工作流计划引用）；
- `skills/**`：用户技能文件；
- `jobs/*.json`：本机作业台账快照。

以下内容**永远不会**上传：WebDAV 密码、模型 API Key、SSH 密码/TOTP 种子、
`approval.key`、`approvals.json`、审计日志、`runs/` 工作流状态，以及白名单外的
任意路径。远端返回的未知路径会被忽略，恶意 WebDAV 服务无法覆盖本机凭据文件。

## 冲突与删除

每次同步在本机 `~/.rsci/webdav/state.json` 记录文件内容哈希和远端 ETag。
只有一边改变时直接采用该边；两边都改变时：

- `settings.json`、`servers.json`、`projects.json`、`jobs/*.json` 按字段/名称合并；
- 同一 `session_id` 的对话按消息去重合并，保留两边新增的消息；
- 其他文件选择较新的一份；
- 原始两边内容保存在 `~/.rsci/webdav/conflicts/`，不会静默丢弃。

删除只对 `chat/` 和 `skills/` 传播：在一个设备删除对话后，下一次同步会删除远端
对应文件，另一设备再同步时删除本地副本。配置、项目索引和作业台账即使远端缺失
也会从本机重新上传，避免一次网络故障或误操作清空配置。

## 命令行

```bash
rsci sync status    # 查看保存的配置和上次同步结果
rsci sync test      # 检查认证与远端目录
rsci sync now       # 手动执行一次双向同步
```

所有命令输出 JSON；失败时返回非零退出码和结构化错误。`sync` 使用与网页控制台
相同的跨进程锁，不会与后台自动同步互相覆盖。

## 常见问题

- **HTTP 401**：用户名或密码错误。Seafile 可能要求使用应用密码；若开启了
  双因素认证，请在门户生成 WebDAV/应用专用密码。
- **HTTP 403**：账号没有该目录的读写权限，换一个 `远端目录` 或在 Seafile 中
  授权该资料库。
- **HTTP 404 / 409**：`/seafdav` 路径不正确，或目标父目录不存在。
- **HTTP 403（根目录创建失败）**：Seafile 不允许在 `/seafdav` 根目录直接
  新建文件夹。把远端目录改成已有资料库下的子目录（本账号可用
  `webdav/RSCI-sync`），或在 Seafile 网页端先建立资料库/文件夹。
- **证书错误**：应用使用系统 CA 校验证书；请修复系统时间/CA，或改为受信任的
  HTTPS 地址。不要使用自签名地址承载账号密码。
- **同步慢**：首次同步会上传全部对话和技能；之后只传输发生变化、且不在本机
  状态文件中的文件。自动同步间隔可按需调大。

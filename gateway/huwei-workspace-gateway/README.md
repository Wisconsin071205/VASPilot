# 远端控制智能体 Workspace Gateway

此单文件组件运行在 Vlab，管理“完整工作区模式”的 rclone SFTP 挂载。它不在
`cl12`、`minus` 等目标服务器部署程序；VS Code Remote-SSH 只进入 Vlab。

## 目录与状态

每个工作区都位于：

```text
~/.huwei-agent/workspaces/<server-id>/<workspace-id>/
  mount/                   # VS Code 在 Vlab 中打开的映射目录
  cache/                   # 独立 rclone 写缓存
  rclone.conf              # 仅目标、用户名、密钥路径和 known_hosts 路径
  rclone.log               # 本工作区日志
  workspace.code-workspace # VS Code 排除大 VASP 数据文件的设置
```

状态只记录路径、工作区 ID、租约、缓存状态和错误摘要；不会记录私钥内容、密码或
动态验证码。rclone 配置必须引用 Vlab 中已有的服务器专用密钥，并强制使用
`~/.ssh/known_hosts` 进行目标服务器主机密钥校验。

## 前提

1. Vlab 已安装 `rclone` 和 FUSE/fusermount。
2. 服务器在 `~/.config/vaspilot/servers.json` 中登记了非宽泛的 `remote_root`。
3. 该服务器已经完成 Vlab 专用密钥登录；目标主机密钥已存在于 Vlab 的
   `~/.ssh/known_hosts`。
4. 先运行 `huwei workspace doctor --server cl12 --path /.../01_relax`。

如果任一前提不满足，继续使用“安全编辑模式”；这不是安装失败。

## 命令

```text
huwei workspace doctor [--server SERVER --path PATH]
huwei workspace open --server SERVER --path PATH [--mode full|read-only]
huwei workspace status [--workspace ws-xxxxxxxx]
huwei workspace list
huwei workspace close --workspace ws-xxxxxxxx [--wait 60]
huwei workspace recover [--workspace ws-xxxxxxxx] [--action list|retry|keep|discard]
huwei workspace cleanup [--apply --confirm CLEANUP-CLOSED-WORKSPACES]
```

`open` 只允许一个具体的、已登记根目录内的计算目录。两个重叠目录不能同时获得
可写租约；第二个请求会被拒绝并提示改用 `--mode read-only`。

`close` 会查询 rclone 写回队列。只要仍有待同步文件或无法读取队列，就拒绝卸载并
保留缓存。`recover` 默认只列出可恢复缓存；`discard` 需要再次完整输入工作区 ID。
`cleanup` 默认是预览，且只清理已关闭会话的 Vlab 本地缓存、日志和状态，从不删除
目标服务器计算文件。

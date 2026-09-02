# 远端控制智能体：单版本 VS Code 远端工作区

本项目只使用一个最新版 VS Code。请不要安装 VS Code 1.85 或为老服务器保留第二个
VS Code。`cl12`、`minus` 等 CentOS 7 服务器不安装 VS Code Server，也不会被
Remote-SSH 直接连接。

## 两种模式

### 安全编辑模式（默认）

适合 `INCAR`、`KPOINTS`、POSCAR、提交脚本和普通文本。

在智能体“文件”页右击文件，选择“在 VS Code 中安全编辑”；右击文件夹，选择
“在 VS Code 中以虚拟目录打开”。扩展经本机控制台、Vlab 和既有目标服务器连接按需
读写：不递归扫描，不下载整个目录。

- 单目录默认前 500 项；可用受限搜索缩小范围。
- 文本文件默认最大 32 MiB。
- `WAVECAR`、`CHGCAR`、`AECCAR0`、`AECCAR2` 不以文本方式打开。
- 保存前比较打开时的 SHA-256；远端文件已变更则拒绝覆盖。
- 保存先写同目录临时文件，校验后原子替换。
- 删除进入智能体回收站。

### Vlab 完整工作区模式

适合需要完整目录、VS Code 终端、语言服务或常规搜索时使用。

在“文件”页右击**具体计算目录**，选择“通过 Vlab 打开完整工作区”。系统会在 Vlab
创建如下目录：

```text
~/.huwei-agent/workspaces/cl12/ws-a13f/mount
```

VS Code Remote-SSH 连接的是 Vlab 的 `huwei-vlab` 别名，随后打开 Vlab 上的
`workspace.code-workspace`；目标目录通过 Vlab 的 rclone SFTP VFS 映射。也就是说，
VS Code Server 只会出现在 Vlab，不会出现在 `cl12/minus`。

旧的“VS Code 直连目标服务器”入口已停用；即使某个旧脚本仍调用它，智能体也会拒绝，
并提示改用安全编辑或 Vlab 完整工作区。

完整工作区采用 `--vfs-cache-mode writes`：普通读取不作为默认磁盘读缓存，修改的
文件进入 Vlab 写缓存，文件关闭并等待写回窗口后上传。每个工作区使用独立缓存目录，
避免重叠远端目录共用 VFS 缓存。

## 一次安装

1. 安装最新版 VS Code。
2. 在项目根目录运行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install-vscode-extension.ps1
   ```

3. 在智能体中配置本机自己的 Vlab PEM 路径和服务器信息。
4. 对将使用完整工作区的服务器完成一次 Vlab 专用密钥配置；目标服务器主机密钥必须
   已由人工确认并保存到 Vlab 的 `known_hosts`。
5. 在本机运行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install-vlab-gateway.ps1
   ```

6. 执行只读检查：

   ```powershell
   huwei workspace doctor --server cl12 --path /home/用户名/calc/01_relax
   ```

另一台电脑只需重新下载项目、安装相同 VSIX、配置**该电脑自己的** Vlab PEM 和服务器
连接，再安装一次 Vlab Gateway。不要复制其他电脑的私钥路径或私钥文件。

## 使用完整工作区

1. 在“文件”页进入某个计算目录，例如 `/home/user/calc/01_relax`。
2. 右击目录，选择“通过 Vlab 打开完整工作区”。
3. 首次 Remote-SSH 连接 Vlab 时，最新版 VS Code 可能在 Vlab 安装 VS Code Server；
   这是预期行为。
4. 在打开的 VS Code 中编辑。工作区级设置已关闭自动保存，并排除了大 VASP 数据文件。
5. 查看底部状态栏：

   ```text
   远端控制智能体: cl12 · 01_relax · 已同步
   ```

   图标含义：勾号为已同步、蓝色旋转图标为上传中、黄色警告为存在未同步修改、锁为
   只读、红色为连接或同步错误。
   点击状态栏可刷新、请求安全关闭或重试恢复。
6. 完成后在主界面“Vlab 工作区空间”卡片点击“关闭”。系统会等待写回完成；若队列
   未清空或无法确认状态，系统拒绝卸载并保留缓存。

## 空间与文件规则

默认 VFS 参数可在 Vlab 的
`~/.huwei-agent/workspaces/config.json` 中修改：

```text
--vfs-cache-mode writes
--vfs-write-back 2s
--vfs-cache-max-size 1GiB
--vfs-cache-max-age 30m
--vfs-cache-min-free-space 2GiB
--dir-cache-time 10s
--attr-timeout 1s
--buffer-size 4MiB
```

- Vlab 使用率达到 80%：主界面黄色警告。
- 达到 90% 或可用空间低于最小写缓存：禁止新建可写工作区；只读工作区仍可尝试。
- 小于等于 32 MiB：适合正常编辑。
- 32–256 MiB：请谨慎打开，优先确认 Vlab 余量。
- 大于 256 MiB：建议只读处理。
- 禁止把 `/`、整个 `/home`、未登记路径或服务器允许根目录外路径挂载为工作区。

缓存上限不是绝对的瞬时硬限制：正在打开的文件不能被 rclone 立即清理。因此务必留出
余量，并优先使用安全编辑模式修改关键输入文件。

## 断线与恢复

如果 Vlab、网络或目标服务器异常断开，系统把工作区标为“需要恢复”，不会静默丢弃
缓存。使用：

```powershell
huwei workspace recover
huwei workspace recover --workspace ws-a13f --action retry
```

`retry` 会以相同缓存目录重新挂载，让 rclone 继续写回；`keep` 仅显示恢复副本位置。
`discard` 是最后选择，必须二次确认工作区 ID。清理命令默认只预览：

```powershell
huwei workspace cleanup
huwei workspace cleanup --apply --confirm CLEANUP-CLOSED-WORKSPACES
```

它只清理 Vlab 本地的缓存、日志和已关闭会话，绝不删除目标服务器中的计算目录。

## 问题排查

**doctor 提示没有 rclone/FUSE**：不要降级 VS Code；继续用安全编辑模式，并请 Vlab
管理员提供 rclone/FUSE。

**提示 Vlab 专用密钥未配置**：完整工作区需要 Vlab 到目标服务器的无交互密钥登录。
按智能体的服务器密钥流程完成一次人工密码/验证码登录即可。

**提示写入租约冲突**：说明已有远端控制智能体创建的重叠可写工作区。关闭前一个，
或将当前目录改为只读打开。

**提示同步未完成，不能关闭**：这是保护行为。保持工作区，稍后重试关闭或使用
`workspace recover`；不要手动删除 Vlab 缓存目录。

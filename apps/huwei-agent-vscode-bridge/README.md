# 胡伟团队专用智能体 VS Code Bridge

通过本地「胡伟团队专用智能体」控制台，在最新版 VS Code 中**按需读写**远端服务器文件。

- **不使用 Remote-SSH**：不在计算服务器上安装 VS Code Server，对老系统（CentOS 7 等）完全友好；
- **按需读取**：展开目录只列直接子项（默认上限 500 条），打开文件才读取内容；
- **安全保存**：以打开时的 SHA-256 为基线，检测远端变化，冲突时拒绝覆盖并保留原文件；
- **回收站**：删除进入智能体回收站，可恢复，不直接永久删除。

## 虚拟 URI

```
huwei-agent-remote://<server-id>/<绝对远端路径>
```

## 唤起链接（由网页控制台右键菜单生成）

```
vscode://huwei-team.huwei-agent-vscode-bridge/open?server=<server-id>&path=<encoded-path>&kind=file|folder
```

## 使用条件

1. 本机已安装并运行「胡伟团队专用智能体」控制台（扩展通过 `%USERPROFILE%\.vaspilot\ui.json` 自动发现地址与令牌）；
2. 目标服务器在控制台中处于「已连接」状态；
3. 本机已安装 VS Code 1.85 及以上。

## 已知限制

- 仅支持文本编辑；`WAVECAR/CHGCAR/AECCAR0/AECCAR2` 等二进制数据默认拒绝打开；
- 文本打开上限默认 32 MiB（可在设置中调整）；
- 虚拟工作区内暂不支持重命名/移动（下一阶段提供）。

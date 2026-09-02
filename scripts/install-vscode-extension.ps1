# 在 Windows 上安装项目自带的最新版 VS Code Bridge。
# 该脚本不安装或下载旧版 VS Code。
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$installer = Join-Path (Split-Path -Parent $scriptRoot) "apps\huwei-agent-vscode-bridge\install-windows.ps1"
if (-not (Test-Path -LiteralPath $installer)) {
    throw "未找到 VS Code Bridge 安装脚本：$installer"
}
& $installer

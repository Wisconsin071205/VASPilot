# 一键安装「胡伟团队专用智能体 VS Code Bridge」扩展（Windows）
# 用法：右键"使用 PowerShell 运行"，或在仓库根目录执行：
#   powershell -ExecutionPolicy Bypass -File apps\huwei-agent-vscode-bridge\install-windows.ps1
$ErrorActionPreference = "Stop"

$extDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent (Split-Path -Parent $extDir)
$vsix = Get-ChildItem -Path (Join-Path $extDir "*.vsix") |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1

if (-not $vsix) {
    Write-Host "[错误] 未找到 .vsix 安装包。请先在本目录执行:" -ForegroundColor Red
    Write-Host "   npm install && npm run compile && npx @vscode/vsce package --allow-missing-repository"
    exit 1
}

$codeCmd = Get-Command code -ErrorAction SilentlyContinue
if (-not $codeCmd) {
    $candidate = "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd"
    if (Test-Path $candidate) { $codeCmd = $candidate } else {
        Write-Host "[错误] 未找到 VS Code（code 命令）。请先安装 VS Code。" -ForegroundColor Red
        exit 1
    }
}

Write-Host "正在安装扩展: $($vsix.Name)"
& $codeCmd --install-extension $vsix.FullName --force
if ($LASTEXITCODE -ne 0) { Write-Host "[错误] 安装失败" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "安装完成。使用前请确认：" -ForegroundColor Green
Write-Host "  1. 「胡伟团队专用智能体」控制台已在本机启动；"
Write-Host "  2. 目标服务器在控制台中处于「已连接」状态；"
Write-Host "  3. 回到控制台的「文件」页，右键文件或文件夹，"
Write-Host "     选择「在 VS Code 中安全编辑 / 以虚拟目录打开」。"

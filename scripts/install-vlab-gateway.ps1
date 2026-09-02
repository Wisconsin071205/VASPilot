# 将 Workspace Gateway 安装到用户自己的 Vlab 帐户。
# 不安装 rclone/FUSE，不修改目标计算服务器，也不接受或保存密码/TOTP。
[CmdletBinding()]
param(
    [string]$HostName = "",
    [string]$UserName = "",
    [int]$Port = 22,
    [string]$IdentityFile = "",
    [switch]$SkipDoctor
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptRoot
$source = Join-Path $repoRoot "gateway\huwei-workspace-gateway\huwei_workspace_gateway.py"
if (-not (Test-Path -LiteralPath $source)) { throw "缺少 Workspace Gateway 源文件：$source" }

if (-not $HostName -or -not $UserName -or -not $IdentityFile) {
    $settingsPath = Join-Path $env:USERPROFILE ".vaspilot\settings.json"
    if (Test-Path -LiteralPath $settingsPath) {
        $settings = Get-Content -Raw -LiteralPath $settingsPath | ConvertFrom-Json
        $vlab = $settings.vlab
        if (-not $HostName) { $HostName = [string]$vlab.host }
        if (-not $UserName) { $UserName = [string]$vlab.user }
        if (-not $IdentityFile) { $IdentityFile = [string]$vlab.identity_file }
        if ($Port -eq 22 -and $vlab.port) { $Port = [int]$vlab.port }
    }
}
if (-not $HostName -or -not $UserName) { throw "请提供 -HostName 和 -UserName，或先配置 Vlab 连接。" }
if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) { throw "找不到 Vlab 私钥：$IdentityFile" }
if (-not (Get-Command ssh -ErrorAction SilentlyContinue) -or -not (Get-Command scp -ErrorAction SilentlyContinue)) {
    throw "需要 Windows OpenSSH 的 ssh 与 scp 命令。"
}

$target = "$UserName@$HostName"
$temporary = "/tmp/huwei-workspace-gateway-$([guid]::NewGuid().ToString('N').Substring(0, 12))"
$sshBase = @("-p", "$Port", "-i", $IdentityFile,
  "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no")

& scp @sshBase $source "$($target):$temporary"
if ($LASTEXITCODE -ne 0) { throw "上传 Workspace Gateway 失败。" }
& ssh @sshBase $target "mkdir -p ~/.huwei-agent/workspaces ~/bin && chmod 700 ~/.huwei-agent ~/.huwei-agent/workspaces 2>/dev/null || true && install -m 700 $temporary ~/bin/huwei-workspace-gateway && rm -f -- $temporary"
if ($LASTEXITCODE -ne 0) { throw "Vlab 安装失败；临时文件未被静默清理。" }

Write-Host "Workspace Gateway 已安装到 $($target):~/bin/huwei-workspace-gateway" -ForegroundColor Green
if (-not $SkipDoctor) {
    Write-Host "正在进行只读检测（不会创建挂载）…"
    & ssh @sshBase $target "~/bin/huwei-workspace-gateway doctor"
    if ($LASTEXITCODE -ne 0) { throw "Gateway doctor 未通过；请根据输出修复 rclone/FUSE/空间问题。" }
}

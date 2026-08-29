# 胡伟团队专用智能体 —— 点击直达控制台
# 已有实例 -> 直接打开最近就绪地址；没有 -> 启动服务（自带自动开浏览器）
$ErrorActionPreference = 'SilentlyContinue'
$log = Join-Path $env:USERPROFILE '.vaspilot\ui.log'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$alive = $false
try {
    $r = Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8930/healthz' -TimeoutSec 2
    $alive = ($r.StatusCode -eq 200)
} catch {}

if ($alive) {
    $ready = Select-String -LiteralPath $log -Pattern 'ready: (\S+)' |
             Select-Object -Last 1
    if ($ready) {
        Start-Process $ready.Matches[0].Groups[1].Value
        exit 0
    }
}
Start-Process powershell -WindowStyle Hidden -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass',
    '-File', (Join-Path $here 'start-ui-detached.ps1'))
# 等服务就绪后打开实际地址（端口被占会自动换邻端口）
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 700
    $ready = Select-String -LiteralPath $log -Pattern 'ready: (\S+)' |
             Select-Object -Last 1
    if ($ready) {
        Start-Process $ready.Matches[0].Groups[1].Value
        exit 0
    }
}
Start-Process 'http://127.0.0.1:8930'

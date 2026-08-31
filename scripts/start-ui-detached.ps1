# Start the VASPilot web console as a detached, minimized process that
# survives the launching shell. Session URL lands in ~/.vaspilot/ui.log
# and the browser opens automatically.
#
# Windows lets several processes bind the same port (SO_REUSEADDR), so a
# previous instance that was never stopped would keep answering with its
# old, now-invalid token ("会话已过期"). Stop every previous instance first.
$log = Join-Path $env:USERPROFILE '.vaspilot\ui.log'
$err = Join-Path $env:USERPROFILE '.vaspilot\ui.err.log'
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -match 'vaspilot ui' -and $_.ProcessId -ne $PID
} | ForEach-Object {
  try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
}
Start-Sleep -Milliseconds 800

Remove-Item $log, $err -ErrorAction SilentlyContinue
Start-Process -FilePath 'py.exe' -ArgumentList '-3.12', '-m', 'vaspilot', 'ui' `
  -WindowStyle Minimized -RedirectStandardOutput $log -RedirectStandardError $err
Write-Output 'detached UI process started'

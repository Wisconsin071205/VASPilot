# Start the VASPilot web console as a detached, minimized process that
# survives the launching shell. Session URL lands in ~/.vaspilot/ui.log
# and the browser opens automatically.
$log = Join-Path $env:USERPROFILE '.vaspilot\ui.log'
$err = Join-Path $env:USERPROFILE '.vaspilot\ui.err.log'
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
Remove-Item $log, $err -ErrorAction SilentlyContinue
Start-Process -FilePath 'py.exe' -ArgumentList '-3.12', '-m', 'vaspilot', 'ui' `
  -WindowStyle Minimized -RedirectStandardOutput $log -RedirectStandardError $err
Write-Output 'detached UI process started'

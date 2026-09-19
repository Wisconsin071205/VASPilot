<#
.SYNOPSIS
  Install the desktop shell for the local console and create shortcuts.

.DESCRIPTION
  Idempotent. Picks a stable CPython >= 3.11 (never an alpha/beta and never
  the Microsoft Store stub), rebuilds .venv on it if the current .venv uses a
  different interpreter, installs the project with the desktop extra, verifies
  pywebview imports, and (re)creates the Desktop and Start Menu shortcuts that
  launch `pythonw -m vaspilot desktop` in the repository directory.

.PARAMETER Python
  Explicit interpreter to use instead of the automatic search.
#>
[CmdletBinding()]
param(
    [string]$Python = ""
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Repo '.venv'
$ShortcutName = '远端控制智能体.lnk'
$Icon = Join-Path $Repo 'src\vaspilot\desktop\assets\icon.ico'

function Test-StablePython([string]$Exe) {
    if (-not $Exe -or -not (Test-Path $Exe)) { return $null }
    try {
        $info = & $Exe -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}.{v.micro} {v.releaselevel}')" 2>$null
    } catch { return $null }
    if ($LASTEXITCODE -ne 0 -or -not $info) { return $null }
    $parts = $info.Trim().Split(' ')
    $ver = [version]$parts[0]
    if ($parts[1] -ne 'final') { return $null }
    if ($ver -lt [version]'3.11.0') { return $null }
    return [pscustomobject]@{ Exe = $Exe; Version = $ver }
}

function Find-StablePython {
    $candidates = @()
    if ($Python) { $candidates += $Python }
    $uvRoot = Join-Path $env:APPDATA 'uv\python'
    if (Test-Path $uvRoot) {
        $candidates += Get-ChildItem $uvRoot -Directory |
            Where-Object { $_.Name -like 'cpython-3.*-windows-*' } |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName 'python.exe' }
    }
    foreach ($tag in '3.13', '3.12', '3.11') {
        try {
            $path = & py "-$tag" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $path -and ($path -notmatch 'WindowsApps')) {
                $candidates += $path.Trim()
            }
        } catch { }
    }
    foreach ($exe in $candidates) {
        $ok = Test-StablePython $exe
        if ($ok) { return $ok }
    }
    throw "No stable CPython >= 3.11 found. Pass -Python C:\path\to\python.exe"
}

$chosen = Find-StablePython
Write-Host "Interpreter: $($chosen.Exe) ($($chosen.Version))"

$rebuild = $true
$cfg = Join-Path $Venv 'pyvenv.cfg'
if (Test-Path $cfg) {
    $line = (Get-Content $cfg | Where-Object { $_ -match '^version\s*=' }) -replace '^version\s*=\s*', ''
    if ($line -and ([version]$line.Trim()) -eq $chosen.Version) { $rebuild = $false }
}
if ($rebuild) {
    if (Test-Path $Venv) {
        Write-Host "Removing .venv (different interpreter)"
        Remove-Item -Recurse -Force $Venv
    }
    Write-Host "Creating .venv"
    & $chosen.Exe -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
} else {
    Write-Host ".venv already on $($chosen.Version); keeping it"
}

$venvPy = Join-Path $Venv 'Scripts\python.exe'
$venvPyw = Join-Path $Venv 'Scripts\pythonw.exe'
Write-Host "Installing project with desktop + dev extras"
& $venvPy -m pip install --disable-pip-version-check -e "$Repo[desktop,dev]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$env:PYTHONNET_RUNTIME = 'netfx'
& $venvPy -c "import importlib.metadata as m, webview; print('pywebview', m.version('pywebview'))"
if ($LASTEXITCODE -ne 0) { throw "pywebview import check failed" }
if (-not (Test-Path $Icon)) { throw "icon missing: $Icon (run scripts\make_icon.py)" }

$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) $ShortcutName),
    (Join-Path ([Environment]::GetFolderPath('Programs')) $ShortcutName)
)
foreach ($lnk in $targets) {
    $sc = $shell.CreateShortcut($lnk)
    $sc.TargetPath = $venvPyw
    $sc.Arguments = '-m vaspilot desktop'
    $sc.WorkingDirectory = $Repo
    $sc.IconLocation = "$Icon,0"
    $sc.Description = 'VASPilot desktop console'
    $sc.WindowStyle = 1
    $sc.Save()
    Write-Host "Shortcut: $lnk"
}
Write-Host "Done. Double-click the shortcut to open the console window."

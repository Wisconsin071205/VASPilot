# Interactive helper: store a provider API key into the USER environment.
# The value is typed blind (no echo), never written to disk by VASPilot,
# and never printed back. New processes pick it up after restart.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\set-provider-key.ps1 VASPILOT_API_KEY_P_GLM
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$EnvVarName
)

if ($EnvVarName -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
    Write-Error "The variable name looks invalid: $EnvVarName"
    exit 1
}

Write-Host "Setting $EnvVarName (user environment)."
Write-Host "Paste the API key and press Enter - input is hidden."
$secure = Read-Host -Prompt "API key" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}
if (-not $plain) {
    Write-Error "Empty input; nothing was changed."
    exit 1
}
[Environment]::SetEnvironmentVariable($EnvVarName, $plain, 'User')
$check = [Environment]::GetEnvironmentVariable($EnvVarName, 'User')
if (-not $check) {
    Write-Error "Save failed; the variable is not present in the user environment."
    exit 1
}
Write-Host ("Saved {0} (length {1}) into the user environment." -f $EnvVarName, $check.Length)
Write-Host "Now RESTART 'vaspilot ui' (close the minimized VASPilot UI window,"
Write-Host "then use the desktop shortcut) so new processes inherit the variable."

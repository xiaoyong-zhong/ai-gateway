[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    & python scripts/check_p0_ip_state.py
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $code

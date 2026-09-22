[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    & python scripts/generate_p0_deploy_report.py
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $code

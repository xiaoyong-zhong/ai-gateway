[CmdletBinding()]
param([string]$Message = 'manual local P0 snapshot')

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    & python scripts/p0_release.py backup --message $Message
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}

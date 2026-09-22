[CmdletBinding()]
param(
    [switch]$VerifyRateLimit,
    [switch]$SkipRateLimit,
    [switch]$UseExplicitLocalHost
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $arguments = @('scripts/run_p0_acceptance.py')
    if (-not $SkipRateLimit) { $arguments += '--verify-rate-limit' }
    if ($UseExplicitLocalHost) { $arguments += '--explicit-host' }
    & python -X utf8 @arguments
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $code

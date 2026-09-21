[CmdletBinding()]
param(
    [switch]$VerifyRateLimit,
    [switch]$UseExplicitLocalHost,
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env.p0-test'
$testScript = Join-Path $root 'scripts/test_p0_gateway.py'
if (-not (Test-Path -LiteralPath $envFile)) {
    throw 'Missing .env.p0-test. Run scripts/Initialize-P0TestEnvironment.ps1 first.'
}

$arguments = @('--env-file', $envFile, '--timeout', $TimeoutSeconds)
if ($VerifyRateLimit) { $arguments += '--verify-rate-limit' }
if ($UseExplicitLocalHost) { $arguments += @('--base-url', 'http://127.0.0.1:18080', '--host', 'ai-gateway-test.local') }
python -X utf8 $testScript @arguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

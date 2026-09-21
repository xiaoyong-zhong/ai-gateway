[CmdletBinding()]
param(
    [switch]$RetireLegacy
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env.p0-test'
$composeFile = Join-Path $root 'deploy/docker-compose.p0-test.yml'
if (-not (Test-Path -LiteralPath $envFile)) {
    throw 'Missing .env.p0-test. Run scripts/Initialize-P0TestEnvironment.ps1 first.'
}

function New-RandomHex([int]$ByteCount) {
    $bytes = [byte[]]::new($ByteCount)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant())
}

$lines = [System.Collections.Generic.List[string]](Get-Content -LiteralPath $envFile)
$values = @{}
foreach ($line in $lines) {
    if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') { $values[$matches[1]] = $matches[2] }
}
if (-not $values.ContainsKey('P0_HIGRESS_CONSUMER_KEY')) {
    throw 'P0_HIGRESS_CONSUMER_KEY is missing from .env.p0-test.'
}

if ($RetireLegacy) {
    $values['P0_HIGRESS_LEGACY_CONSUMER_KEY'] = ''
    $message = 'Removed the legacy Consumer Key from the P0 route.'
} else {
    # At most two keys are accepted: the new key and the immediately previous
    # key. This deliberately prevents an unbounded compatibility-key list.
    $values['P0_HIGRESS_LEGACY_CONSUMER_KEY'] = $values['P0_HIGRESS_CONSUMER_KEY']
    $values['P0_HIGRESS_CONSUMER_KEY'] = 'gw-p0-' + (New-RandomHex 24)
    $message = 'Generated a new P0 Consumer Key and retained the prior P0 key as the temporary legacy key.'
}

$updated = foreach ($line in $lines) {
    if ($line -match '^([A-Z][A-Z0-9_]*)=') {
        $name = $matches[1]
        if ($values.ContainsKey($name)) { "$name=$($values[$name])"; continue }
    }
    $line
}
$updated | Set-Content -LiteralPath $envFile -Encoding ascii

Push-Location $root
try {
    docker compose --env-file .env.p0-test -f $composeFile exec -T higress python3 -X utf8 /p0-scripts/bootstrap_higress_p0.py --env-file /p0-env/.env.p0-test --resource-file /p0-config/resources.json
} finally {
    Pop-Location
}

Write-Host $message
Write-Host 'Read P0_HIGRESS_CONSUMER_KEY from .env.p0-test and update CCSwitch. The key value is intentionally not printed.'

[CmdletBinding()]
param(
    [switch]$Start
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env.p0-test'
$composeFile = Join-Path $root 'deploy/docker-compose.p0-test.yml'

function New-RandomHex([int]$ByteCount) {
    $bytes = [byte[]]::new($ByteCount)
    # PowerShell 5.1/.NET Framework does not expose RandomNumberGenerator.Fill.
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    return ([BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant())
}

if (-not (Test-Path -LiteralPath $envFile)) {
    # The old key is imported only from the local ignored runtime state, solely
    # for the agreed short migration window. It is never written to Git or
    # displayed by this script.
    $legacyKey = ''
    $legacyConfig = Join-Path $root 'runtime/higress/wasmplugins/key-auth.internal.yaml'
    if (Test-Path -LiteralPath $legacyConfig) {
        $match = [regex]::Match((Get-Content -LiteralPath $legacyConfig -Raw), '(?m)^\s*-\s+Bearer\s+([^\s]+)\s*$')
        if ($match.Success) {
            $legacyKey = $match.Groups[1].Value
        }
    }

    @(
        '# P0 local test secrets. Generated locally; ignored by Git.',
        ('P0_POSTGRES_PASSWORD=' + (New-RandomHex 24)),
        ('P0_LITELLM_MASTER_KEY=sk-p0-' + (New-RandomHex 24)),
        ('P0_HIGRESS_CONSUMER_KEY=gw-p0-' + (New-RandomHex 24)),
        ('P0_HIGRESS_LEGACY_CONSUMER_KEY=' + $legacyKey),
        'P0_LOG_RETENTION_DAYS=7'
    ) | Set-Content -LiteralPath $envFile -Encoding ascii
    Write-Host "Created local test secrets: $envFile"
} else {
    Write-Host "Keeping existing local test secrets: $envFile"
}

$hostsLine = '127.0.0.1 ai-gateway-test.local'
Write-Host "Required hosts entry: $hostsLine"
Write-Host 'CCSwitch endpoint: http://ai-gateway-test.local:18080'
Write-Host 'The Higress Consumer Key is P0_HIGRESS_CONSUMER_KEY in .env.p0-test; do not use the LiteLLM Master Key in CCSwitch.'

if ($Start) {
    Push-Location $root
    try {
        docker compose --env-file .env.p0-test -f $composeFile up -d --build
        docker compose --env-file .env.p0-test -f $composeFile exec -T higress python3 -X utf8 /p0-scripts/bootstrap_higress_p0.py --env-file /p0-env/.env.p0-test --resource-file /p0-config/resources.json
    } finally {
        Pop-Location
    }
}

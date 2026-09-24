[CmdletBinding()]
param(
    [switch]$Start,
    [switch]$Provision
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env.p1-test'
$higressEnvFile = Join-Path $root '.env.p1-higress-test'
$appsFile = Join-Path $root 'config/p1-test/apps.json'
$secretDir = Join-Path $root 'runtime/p1-test/secrets'
$secretFile = Join-Path $secretDir 'p1_key_derivation_secret'
$composeFile = Join-Path $root 'deploy/docker-compose.p1-test.yml'

function New-RandomHex([int]$ByteCount) {
    $bytes = [byte[]]::new($ByteCount)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant())
}

New-Item -ItemType Directory -Force -Path $secretDir, (Join-Path $root 'runtime/p1-test/higress') | Out-Null
if (-not (Test-Path -LiteralPath $envFile)) {
    @(
        '# P1 local test compose secrets. Generated locally; ignored by Git.',
        ('P1_POSTGRES_PASSWORD=' + (New-RandomHex 24)),
        ('P1_LITELLM_MASTER_KEY=sk-p1-' + (New-RandomHex 24)),
        'P1_LOG_RETENTION_DAYS=7'
    ) | Set-Content -LiteralPath $envFile -Encoding ascii
    Write-Host "Created $envFile"
}
if (-not (Test-Path -LiteralPath $higressEnvFile)) {
    $keyLines = @('# P1 Higress Consumer keys. Generated locally; ignored by Git.')
    $initialApps = @((Get-Content -LiteralPath $appsFile -Raw -Encoding utf8 | ConvertFrom-Json).applications)
    foreach ($app in $initialApps) {
        $keyLines += ($app.higress_key_env + '=gw-p1-' + (New-RandomHex 24))
    }
    $keyLines | Set-Content -LiteralPath $higressEnvFile -Encoding ascii
    Write-Host "Created $higressEnvFile"
}
if (-not (Test-Path -LiteralPath $secretFile)) {
    $hex = New-RandomHex 32
    $secretBytes = [byte[]]::new(32)
    for ($i = 0; $i -lt $hex.Length; $i += 2) {
        $secretBytes[$i / 2] = [Convert]::ToByte($hex.Substring($i, 2), 16)
    }
    [IO.File]::WriteAllBytes($secretFile, $secretBytes)
    Write-Host "Created local P1 derivation secret (value not displayed)."
}

Write-Host 'Required hosts entry: 127.0.0.1 ai-gateway-p1-test.local'
Write-Host 'CCSwitch endpoint: http://ai-gateway-p1-test.local:28080'
Write-Host "Higress keys are stored in $higressEnvFile; values are not printed."

if ($Start) {
    Push-Location $root
    try {
        docker compose --env-file $envFile -f $composeFile up -d --build
        docker compose --env-file $envFile -f $composeFile exec -T higress python3 -X utf8 /p1-scripts/bootstrap_higress_p1.py --env-file /p1-env/.env.p1-higress-test --resource-file /p1-config/resources.json
        if ($LASTEXITCODE -ne 0) { throw 'P1 Higress bootstrap failed.' }
    } finally { Pop-Location }
    if ($Provision) { & (Join-Path $PSScriptRoot 'Provision-P1VirtualKeys.ps1') }
    $consoleServer = Join-Path $root 'gateway/p1-console/server.py'
    $consoleLogDir = Join-Path $root 'runtime/p1-test/console'
    $consoleUrl = 'http://127.0.0.1:28770/'
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw 'Python was not found. Install Python 3.10 or newer to start the P1 console.'
    }
    New-Item -ItemType Directory -Force -Path $consoleLogDir | Out-Null
    $consoleReady = $false
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri $consoleUrl
        $consoleReady = ([int]$response.StatusCode -eq 200)
    } catch { }
    if (-not $consoleReady) {
        $python = (Get-Command python).Source
        Start-Process -FilePath $python -ArgumentList @($consoleServer) -WorkingDirectory $root -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $consoleLogDir 'console.out.log') `
            -RedirectStandardError (Join-Path $consoleLogDir 'console.err.log') | Out-Null
    }
    Write-Host "P1 console is ready at $consoleUrl"
    try { Start-Process $consoleUrl } catch { Write-Warning "Open the P1 console manually at $consoleUrl" }
}

[CmdletBinding()]
param(
    [switch]$RestartLiteLLM
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root 'deploy/docker-compose.p1-test.yml'
$envFile = Join-Path $root '.env.p1-test'
$higressEnvFile = Join-Path $root '.env.p1-higress-test'
$appsFile = Join-Path $root 'config/p1-test/apps.json'
$reportDir = Join-Path $root 'runtime/p1-test/reports'
$reportFile = Join-Path $reportDir ('p1-native-policy-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.md')
$manageScript = Join-Path $PSScriptRoot 'Manage-P1Application.ps1'

function Get-EnvMap([string]$Path) {
    $map = @{}
    Get-Content -LiteralPath $Path -Encoding ascii | ForEach-Object {
        if ($_ -match '^([^#=]+)=(.*)$') { $map[$matches[1]] = $matches[2] }
    }
    return $map
}

function Invoke-Manage([string]$Action, [object]$App, [int]$Rpm, [int]$Tpm, [int]$Parallel, [string]$ChangedBy) {
    $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $manageScript `
        -Action $Action -AppId $App.app_id -Models @($App.models) -RpmLimit $Rpm `
        -TpmLimit $Tpm -MaxParallelRequests $Parallel -ChangedBy $ChangedBy 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Manage-P1Application failed for $Action" }
    return ($output -join "`n")
}

function Invoke-Chat([string]$Key, [string]$Model) {
    $headers = @{ Authorization = 'Bearer ' + $Key; Host = 'ai-gateway-p1-test.local' }
    $body = @{ model = $Model; messages = @(@{ role = 'user'; content = 'P1 native RPM probe. Reply OK.' }); max_tokens = 1 } | ConvertTo-Json -Compress
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 90 -Uri 'http://127.0.0.1:28080/v1/chat/completions' `
            -Method Post -Headers $headers -ContentType 'application/json' -Body $body
        return [int]$response.StatusCode
    } catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode.value__ }
        return 0
    }
}

function Wait-LiteLLMHealthy {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $health = docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}' api-gateway-p1-test-litellm-1 2>$null
        if ($health -eq 'healthy') { return }
        Start-Sleep -Seconds 3
    }
    throw 'P1 LiteLLM did not become healthy in 90 seconds.'
}

if (-not (Test-Path -LiteralPath $envFile) -or -not (Test-Path -LiteralPath $higressEnvFile)) {
    throw 'Missing P1 environment files. Run Initialize-P1TestEnvironment.ps1 first.'
}

$app = (Get-Content -LiteralPath $appsFile -Raw -Encoding utf8 | ConvertFrom-Json).applications[1]
$keys = Get-EnvMap $higressEnvFile
$clientKey = $keys[$app.higress_key_env]
if ([string]::IsNullOrWhiteSpace($clientKey)) { throw "$($app.higress_key_env) is missing." }

New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
$results = [System.Collections.Generic.List[object]]::new()
$originalRpm = [int]$app.rpm_limit
$originalTpm = [int]$app.tpm_limit
$originalParallel = [int]$app.max_parallel_requests

try {
    Invoke-Manage 'SetPolicy' $app 1 $originalTpm $originalParallel 'p1-native-rpm-probe' | Out-Null
    if ($RestartLiteLLM) {
        docker compose --env-file $envFile -f $composeFile restart litellm | Out-Null
        Wait-LiteLLMHealthy
    }

    $first = Invoke-Chat $clientKey $app.models[0]
    $second = Invoke-Chat $clientKey $app.models[0]
    $results.Add([pscustomobject]@{ Name = 'first request under RPM=1'; Result = if ($first -eq 200) { 'PASS' } else { 'FAIL' }; Evidence = "HTTP $first" })
    $results.Add([pscustomobject]@{ Name = 'second request over RPM=1'; Result = if ($second -eq 429) { 'PASS' } else { 'FAIL' }; Evidence = "HTTP $second" })
} finally {
    Invoke-Manage 'SetPolicy' $app $originalRpm $originalTpm $originalParallel 'p1-native-rpm-restore' | Out-Null
    $reconcile = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $manageScript -Action Reconcile -AppId $app.app_id 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'P1 policy restore reconciliation failed.' }
}

$results | Format-Table -AutoSize
$lines = @(
    '# P1 native policy report',
    '',
    ('Date: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')),
    'Environment: local Docker `api-gateway-p1-test`',
    ('LiteLLM restart for clean window: ' + $RestartLiteLLM),
    '',
    '| Check | Result | Evidence |',
    '|---|---|---|'
)
foreach ($result in $results) { $lines += "| $($result.Name) | $($result.Result) | $($result.Evidence) |" }
$lines += @('', 'The script restores RPM/TPM/max parallel values from apps.json; the report contains no keys.')
Set-Content -LiteralPath $reportFile -Value $lines -Encoding utf8
Write-Host "P1 native policy report: $reportFile"
if (@($results | Where-Object Result -eq 'FAIL').Count -gt 0) { exit 1 }

[CmdletBinding()]
param(
    [string]$ChangedBy = 'p1-local-admin',
    [switch]$SkipSnapshot
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root 'deploy/docker-compose.p1-test.yml'
$envFile = Join-Path $root '.env.p1-test'
$resourceFile = Join-Path $root 'config/higress/p1-test/resources.json'
$snapshotScript = Join-Path $PSScriptRoot 'Snapshot-P1Higress.ps1'

if (-not $SkipSnapshot) {
    & $snapshotScript
    if ($LASTEXITCODE -ne 0) { throw 'Pre-publish Higress snapshot failed.' }
}

docker compose --env-file $envFile -f $composeFile exec -T higress python3 -X utf8 /p1-scripts/bootstrap_higress_p1.py --env-file /p1-env/.env.p1-higress-test --resource-file /p1-config/resources.json
if ($LASTEXITCODE -ne 0) { throw 'P1 Higress bootstrap/publish failed.' }

docker compose --env-file $envFile -f $composeFile restart higress | Out-Null
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    $health = docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}' api-gateway-p1-test-higress-1 2>$null
    if ($health -eq 'healthy') {
        try {
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri 'http://127.0.0.1:28080/v1/models' -Headers @{ Host = 'ai-gateway-p1-test.local' } -ErrorAction Stop | Out-Null
            Start-Sleep -Seconds 5
            break
        } catch {
            if ($_.Exception.Response) { Start-Sleep -Seconds 5; break }
        }
    }
    Start-Sleep -Seconds 3
    if ($attempt -eq 29) { throw 'P1 Higress did not become ready after publish.' }
}

Write-Host "P1 Higress publish completed by $ChangedBy."

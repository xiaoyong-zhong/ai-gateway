[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root 'deploy/docker-compose.p1-test.yml'
$envFile = Join-Path $root '.env.p1-test'

docker compose --env-file $envFile -f $composeFile exec -T mapper python -m unittest discover -s /app -p test_app.py
if ($LASTEXITCODE -ne 0) { throw 'P1 mapper unit tests failed.' }
Write-Host 'P1 mapper test vectors: PASS'

[CmdletBinding()]
param(
    [int]$Port = 28770
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$server = Join-Path $root 'gateway/p1-console/server.py'
$env:P1_CONSOLE_PORT = [string]$Port

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'Python was not found. Install Python 3.10 or newer and ensure python is on PATH.'
}
if (-not (Test-Path -LiteralPath (Join-Path $root '.env.p1-test'))) {
    throw 'Missing .env.p1-test. Run scripts/Initialize-P1TestEnvironment.ps1 first.'
}
if (-not (Test-Path -LiteralPath $server)) {
    throw "Missing P1 console server: $server"
}

Write-Host "P1 local verification console: http://127.0.0.1:$Port/"
Write-Host 'Press Ctrl+C to stop. The console listens on loopback only.'
& python $server

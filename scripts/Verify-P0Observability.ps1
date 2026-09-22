[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$reportDir = Join-Path $root 'runtime/p0-test/reports'
$latest = Get-ChildItem -LiteralPath $reportDir -Filter 'p0-*.json' -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike '*-functional.json' } |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $latest) {
    Write-Error 'No P0 acceptance report exists. Run scripts/Run-P0EndToEnd.ps1 first.'
    exit 1
}

$data = Get-Content -LiteralPath $latest.FullName -Encoding UTF8 -Raw | ConvertFrom-Json
$spend = $data.spendlogs
if ($null -eq $spend -or [int]$spend.logged_requests -le 0 -or
    [int]$spend.request_bodies -le 0 -or [int]$spend.response_bodies -le 0 -or
    [int]$spend.total_tokens -le 0 -or [int]$spend.secret_leak_rows -ne 0) {
    Write-Error "Latest P0 run has incomplete SpendLog evidence: $($latest.Name)"
    exit 1
}

Write-Output "PASS LiteLLM SpendLogs: run_id=$($data.run_id), requests=$($spend.logged_requests), request_bodies=$($spend.request_bodies), response_bodies=$($spend.response_bodies), tokens=$($spend.total_tokens), secret_leak_rows=$($spend.secret_leak_rows)"
Write-Output "UNKNOWN Spend estimate: Qwen unit price has not been provided; stored spend=$($spend.observed_spend) is not a validated price estimate."
Write-Output "Evidence: $($latest.FullName)"

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env.p0-test'
$composeFile = Join-Path $root 'deploy/docker-compose.p0-test.yml'
if (-not (Test-Path -LiteralPath $envFile)) {
    throw 'Missing .env.p0-test. Run scripts/Initialize-P0TestEnvironment.ps1 first.'
}

$envValues = @{}
foreach ($line in Get-Content -LiteralPath $envFile) {
    if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') { $envValues[$matches[1]] = $matches[2] }
}
$providerKey = ''
$providerEnv = Join-Path $root '.env'
if (Test-Path -LiteralPath $providerEnv) {
    foreach ($line in Get-Content -LiteralPath $providerEnv) {
        if ($line -match '^ZHILIN_aigc_API_KEY=(.*)$') { $providerKey = $matches[1]; break }
    }
}

# LiteLLM writes request and response bodies asynchronously. Poll for the P0
# model rather than treating an immediate zero-row query as a logging failure.
$sql = @'
SELECT COUNT(*) AS p0_qwen_rows,
       COUNT(*) FILTER (WHERE proxy_server_request IS NOT NULL) AS request_body_rows,
       COUNT(*) FILTER (WHERE response IS NOT NULL) AS response_body_rows,
       COALESCE(SUM(total_tokens), 0) AS total_tokens,
       COALESCE(SUM(spend), 0) AS estimated_spend,
       COUNT(*) FILTER (WHERE
         COALESCE(proxy_server_request::text, '') LIKE '%' || :'consumer_key' || '%'
         OR COALESCE(proxy_server_request::text, '') LIKE '%' || :'master_key' || '%'
         OR (:'provider_key' <> '' AND COALESCE(proxy_server_request::text, '') LIKE '%' || :'provider_key' || '%')
         OR COALESCE(response::text, '') LIKE '%' || :'consumer_key' || '%'
         OR COALESCE(response::text, '') LIKE '%' || :'master_key' || '%'
         OR (:'provider_key' <> '' AND COALESCE(response::text, '') LIKE '%' || :'provider_key' || '%')
       ) AS secret_leak_rows
FROM "LiteLLM_SpendLogs"
WHERE model_group = 'my-qwen3.6-27b'
  AND "endTime" >= NOW() - INTERVAL '15 minutes';
'@

Push-Location $root
try {
    for ($attempt = 1; $attempt -le 12; $attempt++) {
        $result = $sql | docker compose --env-file .env.p0-test -f $composeFile exec -T db psql -U llmproxy -d litellm -At -F '|' -v "consumer_key=$($envValues['P0_HIGRESS_CONSUMER_KEY'])" -v "master_key=$($envValues['P0_LITELLM_MASTER_KEY'])" -v "provider_key=$providerKey"
        if ($LASTEXITCODE -ne 0) { throw 'Unable to query the isolated LiteLLM database.' }
        $queryValues = $result.Trim().Split('|')
        if ($queryValues.Count -eq 6 -and [int]$queryValues[0] -gt 0 -and [int]$queryValues[1] -gt 0 -and [int]$queryValues[2] -gt 0 -and [int]$queryValues[5] -eq 0) {
            Write-Host "PASS LiteLLM SpendLogs: rows=$($queryValues[0]), request_bodies=$($queryValues[1]), response_bodies=$($queryValues[2]), tokens=$($queryValues[3]), estimated_spend=$($queryValues[4]), secret_leak_rows=$($queryValues[5])"
            exit 0
        }
        Start-Sleep -Seconds 5
    }
    throw 'No recent P0 Qwen request/response bodies found in LiteLLM_SpendLogs after 60 seconds.'
} finally {
    Pop-Location
}

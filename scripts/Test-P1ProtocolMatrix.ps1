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
$manageScript = Join-Path $PSScriptRoot 'Manage-P1Application.ps1'
$reportDir = Join-Path $root 'runtime/p1-test/reports'
$reportFile = Join-Path $reportDir ('p1-protocol-matrix-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.md')

function Get-EnvMap([string]$Path) {
    $map = @{}
    Get-Content -LiteralPath $Path -Encoding ascii | ForEach-Object {
        if ($_ -match '^([^#=]+)=(.*)$') { $map[$matches[1]] = $matches[2] }
    }
    return $map
}

function Invoke-ManagePolicy([object]$App, [int]$Rpm, [int]$Tpm, [int]$Parallel, [string]$ChangedBy) {
    $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $manageScript `
        -Action SetPolicy -AppId $App.app_id -Models @($App.models) -RpmLimit $Rpm `
        -TpmLimit $Tpm -MaxParallelRequests $Parallel -ChangedBy $ChangedBy 2>&1
    if ($LASTEXITCODE -ne 0) { throw "SetPolicy failed for $($App.app_id)." }
    return ($output -join "`n")
}

function Invoke-Request([string]$Key, [string]$Path, [hashtable]$Body, [int]$TimeoutSec = 120) {
    $headers = @{ Authorization = 'Bearer ' + $Key; Host = 'ai-gateway-p1-test.local' }
    $json = $Body | ConvertTo-Json -Compress -Depth 30
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec $TimeoutSec -Uri ('http://127.0.0.1:28080' + $Path) `
            -Method Post -Headers $headers -ContentType 'application/json' -Body $json
        return [pscustomobject]@{ Status = [int]$response.StatusCode; ContentType = [string]$response.Headers['Content-Type']; Body = [string]$response.Content }
    } catch {
        $status = 0; $content = ''
        if ($_.Exception.Response) {
            $status = [int]$_.Exception.Response.StatusCode.value__
            try { $content = (New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())).ReadToEnd() } catch { }
        }
        return [pscustomobject]@{ Status = $status; ContentType = ''; Body = $content }
    }
}

function Invoke-ConcurrentChat([string]$Key, [string]$Model) {
    $env:P1_MATRIX_KEY = $Key
    $env:P1_MATRIX_MODEL = $Model
    $python = @'
import concurrent.futures, json, os, urllib.error, urllib.request
key = os.environ['P1_MATRIX_KEY']
body = json.dumps({'model': os.environ['P1_MATRIX_MODEL'], 'messages': [{'role': 'user', 'content': 'P1 max parallel probe. Explain briefly.'}], 'max_tokens': 8}).encode()
def call(_):
    req = urllib.request.Request('http://127.0.0.1:28080/v1/chat/completions', data=body, method='POST', headers={'Authorization': 'Bearer '+key, 'Host': 'ai-gateway-p1-test.local', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=120) as r: return r.status
    except urllib.error.HTTPError as e: return e.code
    except Exception: return 0
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    print(json.dumps(list(pool.map(call, [1, 2]))))
'@
    try { return (($python | python -) -join "`n") | ConvertFrom-Json }
    finally { Remove-Item Env:P1_MATRIX_KEY,Env:P1_MATRIX_MODEL -ErrorAction SilentlyContinue }
}

function Get-AppKeyHash([byte[]]$Secret, [string]$AppId) {
    $hmac = [Security.Cryptography.HMACSHA256]::new($Secret)
    try { $digest = $hmac.ComputeHash([Text.Encoding]::ASCII.GetBytes('campus-ai-gateway/litellm-key/v1/' + $AppId)) }
    finally { $hmac.Dispose() }
    $key = 'sk-' + [Convert]::ToBase64String($digest).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::ASCII.GetBytes($key))).Replace('-', '').ToLowerInvariant()) }
    finally { $sha.Dispose() }
}

function Get-SpendEvidence([string]$ApiKeyHash) {
    $sql = @'
SELECT COUNT(*)::text || '|' || COUNT(*) FILTER (WHERE messages IS NOT NULL)::text || '|' || COUNT(*) FILTER (WHERE response IS NOT NULL)::text
FROM "LiteLLM_SpendLogs" WHERE api_key = :'appkey';
'@
    $args = @('--env-file', $envFile, '-f', $composeFile, 'exec', '-T', 'db', 'psql', '-U', 'llmproxy', '-d', 'litellm', '-At', '-v', ('appkey=' + $ApiKeyHash))
    $output = $sql | docker compose @args
    if ($LASTEXITCODE -ne 0) { throw 'SpendLog query failed.' }
    return (($output -join "`n").Trim())
}

function Wait-LiteLLMHealthy {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $health = docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}' api-gateway-p1-test-litellm-1 2>$null
        if ($health -eq 'healthy') { return }
        Start-Sleep -Seconds 3
    }
    throw 'P1 LiteLLM did not become healthy in 90 seconds.'
}

$apps = (Get-Content -LiteralPath $appsFile -Raw -Encoding utf8 | ConvertFrom-Json).applications
$appA = $apps | Where-Object app_id -eq 'app-401f743d-0c35-4e10-bf2c-7e6071e813a1'
$appB = $apps | Where-Object app_id -eq 'app-979432ae-7b09-4fdc-91d8-5d02d1c82a69'
$keys = Get-EnvMap $higressEnvFile
$keyA = $keys[$appA.higress_key_env]; $keyB = $keys[$appB.higress_key_env]
if ([string]::IsNullOrWhiteSpace($keyA) -or [string]::IsNullOrWhiteSpace($keyB)) { throw 'P1 App A/B Key-H is missing.' }
$results = [System.Collections.Generic.List[object]]::new()
$originalA = @{ Rpm = [int]$appA.rpm_limit; Tpm = [int]$appA.tpm_limit; Parallel = [int]$appA.max_parallel_requests }
$originalB = @{ Rpm = [int]$appB.rpm_limit; Tpm = [int]$appB.tpm_limit; Parallel = [int]$appB.max_parallel_requests }
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null

try {
    if ($RestartLiteLLM) {
        docker compose --env-file $envFile -f $composeFile restart litellm | Out-Null
        Wait-LiteLLMHealthy
    }
    Invoke-ManagePolicy $appB 100 1 1 'p1-tpm-probe' | Out-Null
    $tpm = Invoke-Request $keyB '/v1/chat/completions' @{ model = $appB.models[0]; messages = @(@{ role = 'user'; content = 'P1 TPM probe with enough words to exceed the one token test quota.' }); max_tokens = 1 }
    $results.Add([pscustomobject]@{ Name = 'TPM quota rejection'; Result = if ($tpm.Status -eq 429) { 'PASS' } else { 'FAIL' }; Evidence = "HTTP $($tpm.Status) with tpm_limit=1" })
    Invoke-ManagePolicy $appB $originalB.Rpm $originalB.Tpm $originalB.Parallel 'p1-tpm-restore' | Out-Null

    Invoke-ManagePolicy $appA 100 $originalA.Tpm 1 'p1-concurrency-probe' | Out-Null
    $parallel = Invoke-ConcurrentChat $keyA $appA.models[0]
    $parallelText = (@($parallel) -join ',')
    $results.Add([pscustomobject]@{ Name = 'max_parallel_requests=1'; Result = if (@($parallel) -contains 429) { 'PASS' } else { 'FAIL' }; Evidence = "HTTP [$parallelText]" })
    Invoke-ManagePolicy $appA $originalA.Rpm $originalA.Tpm $originalA.Parallel 'p1-protocol-restore' | Out-Null

    $sse = Invoke-Request $keyA '/v1/chat/completions' @{ model = $appA.models[0]; messages = @(@{ role = 'user'; content = 'P1 SSE probe. Reply briefly.' }); max_tokens = 2; stream = $true }
    $ssePass = ($sse.Status -eq 200 -and $sse.ContentType -match 'text/event-stream' -and $sse.Body -match '\[DONE\]')
    $results.Add([pscustomobject]@{ Name = 'SSE chat completions'; Result = if ($ssePass) { 'PASS' } else { 'FAIL' }; Evidence = "HTTP $($sse.Status), Content-Type=$($sse.ContentType), done=$($sse.Body -match '\[DONE\]')" })

    $responses = Invoke-Request $keyA '/v1/responses' @{ model = $appA.models[0]; input = 'P1 Responses probe. Reply briefly.'; max_output_tokens = 2 }
    $results.Add([pscustomobject]@{ Name = 'Responses API'; Result = if ($responses.Status -eq 200) { 'PASS' } else { 'FAIL' }; Evidence = "HTTP $($responses.Status)" })

    $tool = Invoke-Request $keyA '/v1/chat/completions' @{ model = $appA.models[0]; messages = @(@{ role = 'user'; content = 'Call the probe tool.' }); tools = @(@{ type = 'function'; function = @{ name = 'probe'; description = 'Return a probe result'; parameters = @{ type = 'object'; properties = @{} } } }); tool_choice = @{ type = 'function'; function = @{ name = 'probe' } }; max_tokens = 8 }
    $results.Add([pscustomobject]@{ Name = 'Tools/function call passthrough'; Result = if ($tool.Status -eq 200) { 'PASS' } else { 'FAIL' }; Evidence = "HTTP $($tool.Status)" })

    Start-Sleep -Seconds 3
    $secret = [IO.File]::ReadAllBytes((Join-Path $root 'runtime/p1-test/secrets/p1_key_derivation_secret'))
    $hash = Get-AppKeyHash $secret $appA.app_id
    $spend = Get-SpendEvidence $hash
    $parts = $spend -split '\|'
    $spendPass = ($parts.Count -eq 3 -and [int]$parts[0] -gt 0 -and [int]$parts[1] -gt 0 -and [int]$parts[2] -gt 0)
    $results.Add([pscustomobject]@{ Name = 'SpendLog estimated usage'; Result = if ($spendPass) { 'PASS' } else { 'FAIL' }; Evidence = "rows/messages/responses=$spend; api_key verified by SHA-256 lookup" })
} finally {
    try { Invoke-ManagePolicy $appA $originalA.Rpm $originalA.Tpm $originalA.Parallel 'p1-matrix-restore-a' | Out-Null } catch { Write-Warning $_.Exception.Message }
    try { Invoke-ManagePolicy $appB $originalB.Rpm $originalB.Tpm $originalB.Parallel 'p1-matrix-restore-b' | Out-Null } catch { Write-Warning $_.Exception.Message }
}

$results | Format-Table -AutoSize
$lines = @(
    '# P1 protocol and native governance matrix',
    '',
    ('Date: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')),
    'Environment: local Docker `api-gateway-p1-test`',
    'The script uses LiteLLM native policy enforcement and SpendLog; it does not implement a second quota or billing layer.',
    '',
    '| Check | Result | Evidence |',
    '|---|---|---|'
)
foreach ($result in $results) { $lines += "| $($result.Name) | $($result.Result) | $($result.Evidence) |" }
$lines += @('', 'No API key plaintext was written to this report.')
Set-Content -LiteralPath $reportFile -Value $lines -Encoding utf8
Write-Host "P1 protocol matrix report: $reportFile"
if (@($results | Where-Object Result -eq 'FAIL').Count -gt 0) { exit 1 }

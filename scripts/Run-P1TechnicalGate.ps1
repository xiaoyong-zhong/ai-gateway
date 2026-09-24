[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$compose = Join-Path $root 'deploy/docker-compose.p1-test.yml'
$envFile = Join-Path $root '.env.p1-test'
$higressEnv = Join-Path $root '.env.p1-higress-test'
$secretFile = Join-Path $root 'runtime/p1-test/secrets/p1_key_derivation_secret'
$reportDir = Join-Path $root 'runtime/p1-test/reports'
$reportFile = Join-Path $reportDir ('p1-technical-gate-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.md')

function Get-EnvMap([string]$Path) {
    $map = @{}
    Get-Content -LiteralPath $Path -Encoding ascii | ForEach-Object {
        if ($_ -match '^([^#=]+)=(.*)$') { $map[$matches[1]] = $matches[2] }
    }
    return $map
}

function Get-DerivedKey([byte[]]$Secret, [string]$AppId) {
    $hmac = [Security.Cryptography.HMACSHA256]::new($Secret)
    try { $digest = $hmac.ComputeHash([Text.Encoding]::ASCII.GetBytes('campus-ai-gateway/litellm-key/v1/' + $AppId)) }
    finally { $hmac.Dispose() }
    return 'sk-' + [Convert]::ToBase64String($digest).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Invoke-Chat([string]$Key, [string]$Model, [string]$RequestHost = 'ai-gateway-p1-test.local', [hashtable]$ExtraHeaders = @{}) {
    $headers = @{ Authorization = 'Bearer ' + $Key; Host = $RequestHost }
    foreach ($name in $ExtraHeaders.Keys) { $headers[$name] = $ExtraHeaders[$name] }
    $body = @{ model = $Model; messages = @(@{ role = 'user'; content = 'P1 technical gate. Reply OK.' }) } | ConvertTo-Json -Compress
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 90 -Uri 'http://127.0.0.1:28080/v1/chat/completions' -Method Post -Headers $headers -ContentType 'application/json' -Body $body
        return [pscustomobject]@{ Status = [int]$response.StatusCode; HasSensitiveResponseHeader = (($response.Headers.Keys | Where-Object { $_ -match '(?i)authorization|p1-litellm|p1-application' }).Count -gt 0) }
    } catch {
        $status = 0
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode.value__ }
        return [pscustomobject]@{ Status = $status; HasSensitiveResponseHeader = $false }
    }
}

function Add-Result([System.Collections.Generic.List[object]]$Results, [string]$Name, [bool]$Passed, [string]$Evidence) {
    $Results.Add([pscustomobject]@{ Name = $Name; Result = if ($Passed) { 'PASS' } else { 'FAIL' }; Evidence = $Evidence })
}

$results = [System.Collections.Generic.List[object]]::new()
$keys = Get-EnvMap $higressEnv
$secret = [IO.File]::ReadAllBytes($secretFile)
$apps = @((Get-Content -LiteralPath (Join-Path $root 'config/p1-test/apps.json') -Raw -Encoding utf8 | ConvertFrom-Json).applications)
$appA = 'app-401f743d-0c35-4e10-bf2c-7e6071e813a1'
$appB = 'app-979432ae-7b09-4fdc-91d8-5d02d1c82a69'
$appAConfig = $apps | Where-Object app_id -eq $appA
$appBConfig = $apps | Where-Object app_id -eq $appB

$a = Invoke-Chat $keys[$appAConfig.higress_key_env] 'my-qwen3.6-27b'
Add-Result $results 'app-a authorized Qwen request' ($a.Status -eq 200 -and -not $a.HasSensitiveResponseHeader) "HTTP $($a.Status); response sensitive headers=$($a.HasSensitiveResponseHeader)"
$b = Invoke-Chat $keys[$appBConfig.higress_key_env] 'my-qwen3.6-27b'
Add-Result $results 'app-b authorized Qwen request' ($b.Status -eq 200 -and -not $b.HasSensitiveResponseHeader) "HTTP $($b.Status); response sensitive headers=$($b.HasSensitiveResponseHeader)"
$deniedModel = Invoke-Chat $keys[$appAConfig.higress_key_env] 'not-authorized-model'
Add-Result $results 'LiteLLM model policy rejection' ($deniedModel.Status -eq 403) "HTTP $($deniedModel.Status)"
$invalid = Invoke-Chat 'invalid-p1-client-key' 'my-qwen3.6-27b'
Add-Result $results 'invalid Higress key rejection' ($invalid.Status -eq 401) "HTTP $($invalid.Status)"
$wrongHost = Invoke-Chat $keys[$appAConfig.higress_key_env] 'my-qwen3.6-27b' 'wrong-p1.local'
Add-Result $results 'wrong Host rejection' ($wrongHost.Status -eq 403) "HTTP $($wrongHost.Status)"
$clientLiteLLMKey = Get-DerivedKey $secret $appA
$bypass = Invoke-Chat $clientLiteLLMKey 'my-qwen3.6-27b'
Add-Result $results 'LiteLLM Key cannot authenticate at Higress' ($bypass.Status -eq 401) "HTTP $($bypass.Status)"
$spoof = Invoke-Chat $keys[$appAConfig.higress_key_env] 'my-qwen3.6-27b' 'ai-gateway-p1-test.local' @{'X-Mse-Consumer' = $appB}
$latestMapperLog = docker compose --env-file $envFile -f $compose logs --tail=1 --no-color mapper 2>$null | Out-String
Add-Result $results 'spoofed Consumer header is ignored' ($spoof.Status -eq 200 -and $latestMapperLog -match [regex]::Escape("authorize consumer=$appA")) "HTTP $($spoof.Status); mapper received authenticated app-a"

docker compose --env-file $envFile -f $compose stop mapper | Out-Null
try {
    $down = Invoke-Chat $keys[$appAConfig.higress_key_env] 'my-qwen3.6-27b'
    Add-Result $results 'mapper outage fails closed' ($down.Status -eq 403) "HTTP $($down.Status)"
} finally {
    docker compose --env-file $envFile -f $compose start mapper | Out-Null
    Start-Sleep -Seconds 3
}

$litellmPorts = docker port api-gateway-p1-test-litellm-1 2>$null | Out-String
$mapperPorts = docker port api-gateway-p1-test-mapper-1 2>$null | Out-String
Add-Result $results 'LiteLLM has no host published port' ([string]::IsNullOrWhiteSpace($litellmPorts)) 'docker port returned no mapping'
Add-Result $results 'mapper has no host published port' ([string]::IsNullOrWhiteSpace($mapperPorts)) 'docker port returned no mapping'

$p0Status = docker compose --env-file (Join-Path $root '.env.p0-test') -f (Join-Path $root 'deploy/docker-compose.p0-test.yml') ps --format '{{.Name}} {{.Status}}' 2>$null | Out-String
$p0Healthy = ($p0Status -split "`r?`n" | Where-Object { $_ -and $_ -notmatch 'healthy|Up' }).Count -eq 0
Add-Result $results 'P0 containers remain healthy' $p0Healthy 'P0 compose status checked without restart'

New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
$lines = @('# P1-0 technical gate report', '', ('Generated at: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')), '', '| Check | Result | Evidence |', '|---|---|---|')
foreach ($item in $results) { $lines += "| $($item.Name) | $($item.Result) | $($item.Evidence.Replace('|','/')) |" }
$lines += ''
$gateConclusion = 'FAIL'
if (($results | Where-Object Result -eq 'FAIL').Count -eq 0) { $gateConclusion = 'PASS' }
$lines += ('Conclusion: ' + $gateConclusion)
$lines | Set-Content -LiteralPath $reportFile -Encoding utf8
Write-Host "P1 technical gate report: $reportFile"
$results | Format-Table -AutoSize
if (($results | Where-Object Result -eq 'FAIL').Count -gt 0) { exit 1 }

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DisplayName,
    [string[]]$Models,
    [string]$ModelsB64,
    [int]$RpmLimit = 6,
    [int]$TpmLimit = 6000,
    [int]$MaxParallelRequests = 1,
    [int]$VerifyTimeoutSec = 90,
    [string]$AppId,
    [string]$ChangedBy = 'p1-local-admin',
    [switch]$RollbackAfterVerify
)

$ErrorActionPreference = 'Stop'
$Models = if ($ModelsB64) { @([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($ModelsB64)) | ConvertFrom-Json) } else { @($Models) }
$Models = @($Models | ForEach-Object { $value = ([string]$_).Trim('"'); if ($value.Contains(',')) { $value.Split(',') } else { $value } } | ForEach-Object { ([string]$_).Trim('"').Trim() } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root 'deploy/docker-compose.p1-test.yml'
$envFile = Join-Path $root '.env.p1-test'
$higressEnvFile = Join-Path $root '.env.p1-higress-test'
$appsFile = Join-Path $root 'config/p1-test/apps.json'
$resourceTemplateFile = Join-Path $root 'config/higress/p1-test/resources.json'
$secretFile = Join-Path $root 'runtime/p1-test/secrets/p1_key_derivation_secret'
$stateDir = Join-Path $root 'runtime/p1-test/state'
$rotationDir = Join-Path $root 'runtime/p1-test/rotations'
$reportDir = Join-Path $root 'runtime/p1-test/reports'
$lockFile = Join-Path $stateDir 'application-create.lock'

function Get-DerivedKey([byte[]]$Secret, [string]$Id) {
    $hmac = [Security.Cryptography.HMACSHA256]::new($Secret)
    try { $digest = $hmac.ComputeHash([Text.Encoding]::ASCII.GetBytes('campus-ai-gateway/litellm-key/v1/' + $Id)) }
    finally { $hmac.Dispose() }
    return 'sk-' + [Convert]::ToBase64String($digest).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function New-KeyH {
    $bytes = [byte[]]::new(32)
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return 'gw-p1-' + ([Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_'))
}

function Get-EnvMap([string]$Path) {
    $map = @{}
    Get-Content -LiteralPath $Path -Encoding ascii | ForEach-Object {
        if ($_ -match '^([^#=]+)=(.*)$') { $map[$matches[1]] = $matches[2] }
    }
    return $map
}

function Get-KeyEnvName([string]$Id) {
    if ([string]::IsNullOrWhiteSpace($Id)) { throw 'Cannot derive a Higress key environment variable without app_id.' }
    $suffix = ($Id.ToUpperInvariant() -replace '[^A-Z0-9]', '_')
    return 'P1_HIGRESS_KEY_' + $suffix + '_KEY'
}

function Get-HigressResource {
    $python = @'
import json, ssl, urllib.request
url = "https://127.0.0.1:18443/apis/extensions.higress.io/v1alpha1/namespaces/higress-system/wasmplugins/p1-key-auth"
request = urllib.request.Request(url, method="GET")
with urllib.request.urlopen(request, context=ssl._create_unverified_context(), timeout=10) as response:
    print(json.dumps(json.load(response), separators=(",", ":")))
'@
    $output = $python | docker compose --env-file $envFile -f $composeFile exec -T higress python3 -
    if ($LASTEXITCODE -ne 0) { throw 'Failed to read the P1 Higress key-auth resource.' }
    return ($output -join "`n")
}

function Put-HigressResource([string]$ResourceJson) {
    $env:P1_HIGRESS_RESOURCE_B64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($ResourceJson))
    $python = @'
import base64, json, os, ssl, urllib.request
from urllib.error import HTTPError
resource = json.loads(base64.b64decode(os.environ["P1_HIGRESS_RESOURCE_B64"]))
url = "https://127.0.0.1:18443/apis/extensions.higress.io/v1alpha1/namespaces/higress-system/wasmplugins/p1-key-auth"
ctx = ssl._create_unverified_context()
def put(value):
    data = json.dumps(value, separators=(",", ":")).encode()
    request = urllib.request.Request(url, data=data, method="PUT", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, context=ctx, timeout=10) as response:
        if response.status not in (200, 201):
            raise SystemExit("unexpected Higress status")
try:
    put(resource)
except HTTPError as error:
    if error.code != 409:
        raise
    # A previous PUT may have advanced resourceVersion. Refresh it once and
    # retry so rollback remains valid after a concurrent controller update.
    with urllib.request.urlopen(urllib.request.Request(url, method="GET"), context=ctx, timeout=10) as response:
        current = json.load(response)
    resource.setdefault("metadata", {})["resourceVersion"] = current.get("metadata", {}).get("resourceVersion")
    put(resource)
print("Higress key-auth resource applied")
'@
    try {
        $output = $python | docker compose --env-file $envFile -f $composeFile exec -T -e P1_HIGRESS_RESOURCE_B64 higress python3 -
        if ($LASTEXITCODE -ne 0) { throw 'Failed to apply the P1 Higress key-auth resource.' }
        Write-Host ($output -join "`n")
    } finally { Remove-Item Env:P1_HIGRESS_RESOURCE_B64 -ErrorAction SilentlyContinue }
}

function Invoke-LiteLLMEnsure([hashtable]$Payload) {
    $json = $Payload | ConvertTo-Json -Compress -Depth 8
    $env:P1_NEW_APP_B64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
    $python = @'
import base64, json, os, urllib.parse, urllib.request
from urllib.error import HTTPError

p = json.loads(base64.b64decode(os.environ["P1_NEW_APP_B64"]))
key = p["key"]
headers = {"Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"], "Content-Type": "application/json", "litellm-changed-by": p.get("changed_by", "p1-local-admin")}
base = "http://127.0.0.1:4000"
info_url = base + "/key/info?" + urllib.parse.urlencode({"key": key})
existing = None
created = False
try:
    with urllib.request.urlopen(urllib.request.Request(info_url, method="GET", headers=headers), timeout=20) as response:
        existing = json.load(response).get("info", {})
except HTTPError as error:
    if error.code != 404:
        raise

if existing is None:
    if not p.get("create_if_missing", True):
        raise SystemExit("LiteLLM Virtual Key does not exist")
    body = {
        "key": key,
        "key_alias": p["app_id"],
        "key_type": "llm_api",
        "models": p["models"],
        "rpm_limit": p["rpm_limit"],
        "tpm_limit": p["tpm_limit"],
        "max_parallel_requests": p["max_parallel_requests"],
        "metadata": {"app_id": p["app_id"], "p1": "application-create"},
    }
    request = urllib.request.Request(base + "/key/generate", data=json.dumps(body).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status not in (200, 201):
            raise RuntimeError("unexpected LiteLLM key/generate status")
    with urllib.request.urlopen(urllib.request.Request(info_url, method="GET", headers=headers), timeout=20) as response:
        existing = json.load(response).get("info", {})
    created = True
state = {name: existing.get(name) for name in ("key_alias", "models", "rpm_limit", "tpm_limit", "max_parallel_requests", "blocked")}
print(json.dumps({"state": "created" if created else "existing", "info": state}, ensure_ascii=False, sort_keys=True))
'@
    try {
        $output = $python | docker compose --env-file $envFile -f $composeFile exec -T -e P1_NEW_APP_B64 litellm python -
        if ($LASTEXITCODE -ne 0) { throw 'LiteLLM Virtual Key creation or reconciliation failed.' }
        return ($output -join "`n")
    } finally { Remove-Item Env:P1_NEW_APP_B64 -ErrorAction SilentlyContinue }
}

function Invoke-LiteLLMBlock([string]$Key, [string]$ChangedBy) {
    $payload = @{ key = $Key; changed_by = $ChangedBy } | ConvertTo-Json -Compress
    $env:P1_BLOCK_APP_B64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payload))
    $python = @'
import base64, json, os, urllib.request
p = json.loads(base64.b64decode(os.environ["P1_BLOCK_APP_B64"]))
headers = {"Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"], "Content-Type": "application/json", "litellm-changed-by": p.get("changed_by", "p1-local-admin")}
request = urllib.request.Request("http://127.0.0.1:4000/key/block", data=json.dumps({"key": p["key"]}).encode(), method="POST", headers=headers)
with urllib.request.urlopen(request, timeout=20) as response:
    if response.status not in (200, 201):
        raise SystemExit("unexpected LiteLLM key/block status")
print("LiteLLM Key-L blocked")
'@
    try {
        $output = $python | docker compose --env-file $envFile -f $composeFile exec -T -e P1_BLOCK_APP_B64 litellm python -
        if ($LASTEXITCODE -ne 0) { throw 'LiteLLM Key-L block failed during rollback.' }
        Write-Host ($output -join "`n")
    } finally { Remove-Item Env:P1_BLOCK_APP_B64 -ErrorAction SilentlyContinue }
}

function Invoke-ChatStatus([string]$Key, [string]$Model) {
    $headers = @{ Authorization = 'Bearer ' + $Key; Host = 'ai-gateway-p1-test.local' }
    $body = @{ model = $Model; messages = @(@{ role = 'user'; content = 'P1 application creation probe. Reply OK.' }); max_tokens = 1 } | ConvertTo-Json -Compress
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec $VerifyTimeoutSec -Uri 'http://127.0.0.1:28080/v1/chat/completions' -Method Post -Headers $headers -ContentType 'application/json' -Body $body
        return [int]$response.StatusCode
    } catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode.value__ }
        return 0
    }
}

function Invoke-VerifiedStatus([string]$Key, [string]$Model, [int]$Expected) {
    $last = 0
    for ($attempt = 0; $attempt -lt 6; $attempt++) {
        $last = Invoke-ChatStatus $Key $Model
        if ($last -eq $Expected) { return $last }
        if ($attempt -lt 5) { Start-Sleep -Seconds 5 }
    }
    return $last
}

function Restart-MapperAndWait {
    docker compose --env-file $envFile -f $composeFile restart mapper | Out-Null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $health = docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}' api-gateway-p1-test-mapper-1 2>$null
        if ($health -eq 'healthy') { return }
        Start-Sleep -Seconds 2
    }
    throw 'P1 mapper did not become healthy in 60 seconds.'
}

function Restart-HigressAndWait {
    docker compose --env-file $envFile -f $composeFile restart higress | Out-Null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $health = docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}' api-gateway-p1-test-higress-1 2>$null
        if ($health -eq 'healthy') {
            try {
                Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri 'http://127.0.0.1:28080/v1/models' -Headers @{ Host = 'ai-gateway-p1-test.local' } -ErrorAction Stop | Out-Null
                Start-Sleep -Seconds 5
                return
            } catch {
                if ($_.Exception.Response) { Start-Sleep -Seconds 5; return }
            }
        }
        Start-Sleep -Seconds 3
    }
    throw 'P1 Higress did not become healthy in 90 seconds.'
}

if (-not (Test-Path -LiteralPath $envFile) -or -not (Test-Path -LiteralPath $higressEnvFile) -or -not (Test-Path -LiteralPath $secretFile)) {
    throw 'Missing P1 environment files. Run Initialize-P1TestEnvironment.ps1 first.'
}
if (-not $Models -or $Models.Count -eq 0) { throw 'At least one model is required.' }
if ($RpmLimit -le 0 -or $TpmLimit -le 0 -or $MaxParallelRequests -le 0) { throw 'RPM, TPM and max parallel requests must be positive.' }
if ([string]::IsNullOrWhiteSpace($AppId)) { $AppId = [Guid]::NewGuid().ToString() }
$guidText = $AppId
if ($guidText.StartsWith('app-', [StringComparison]::OrdinalIgnoreCase)) { $guidText = $guidText.Substring(4) }
$parsedGuid = [Guid]::Empty
if (-not [Guid]::TryParse($guidText, [ref]$parsedGuid)) { throw "Invalid app_id: $AppId" }
$AppId = 'app-' + $parsedGuid.ToString()

New-Item -ItemType Directory -Force -Path $stateDir, $rotationDir, $reportDir | Out-Null
$lock = $null
$originalAppsText = Get-Content -LiteralPath $appsFile -Raw -Encoding utf8
$originalResourcesText = Get-Content -LiteralPath $resourceTemplateFile -Raw -Encoding utf8
$originalHigressEnvText = Get-Content -LiteralPath $higressEnvFile -Raw -Encoding ascii
$appsJson = $originalAppsText | ConvertFrom-Json
$applications = @($appsJson.applications)
$existingApp = $applications | Where-Object { $_.app_id -eq $AppId }
if ($existingApp) {
    $same = ($existingApp.display_name -eq $DisplayName -and (@($existingApp.models) -join ',') -eq (@($Models) -join ',') -and [int]$existingApp.rpm_limit -eq $RpmLimit -and [int]$existingApp.tpm_limit -eq $TpmLimit -and [int]$existingApp.max_parallel_requests -eq $MaxParallelRequests)
    if ($same) {
        $secretForIdempotency = [IO.File]::ReadAllBytes($secretFile)
        $keyForIdempotency = Get-DerivedKey $secretForIdempotency $AppId
        try {
            $existingInfo = Invoke-LiteLLMEnsure @{ app_id = $AppId; key = $keyForIdempotency; models = @($Models); rpm_limit = $RpmLimit; tpm_limit = $TpmLimit; max_parallel_requests = $MaxParallelRequests; changed_by = $ChangedBy; create_if_missing = $false } | ConvertFrom-Json
            $liveResource = Get-HigressResource | ConvertFrom-Json
            $liveConsumer = @($liveResource.spec.defaultConfig.consumers) | Where-Object { $_.name -eq $AppId }
            if ($existingInfo.state -ne 'existing' -or -not $liveConsumer) { throw 'runtime declaration is incomplete' }
            Write-Host "P1 application already exists and runtime declaration is consistent: $AppId"
            exit 0
        } catch {
            throw "P1 app_id exists in apps.json but runtime consistency check failed: $($_.Exception.Message)"
        }
    }
    throw "P1 app_id already exists with a different declaration: $AppId"
}

$envName = Get-KeyEnvName $AppId
$keyH = New-KeyH
$secret = [IO.File]::ReadAllBytes($secretFile)
$keyL = Get-DerivedKey $secret $AppId
$higressResourceJson = $null
$liteLLMKeyCreated = $false
$higressApplied = $false
$appsCommitted = $false
$resourceTemplateCommitted = $false
$envCommitted = $false
$transactionId = Get-Date -Format 'yyyyMMdd-HHmmss'
$rawSnapshotPath = Join-Path $stateDir ('application-create-' + $AppId + '-' + $transactionId + '-higress.json')
$keyOutputPath = Join-Path $rotationDir ($AppId + '-created-' + $transactionId + '.key')

try {
    $lock = [IO.File]::Open($lockFile, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $higressResourceJson = Get-HigressResource
    Set-Content -LiteralPath $rawSnapshotPath -Value $higressResourceJson -Encoding utf8
    $higressResource = $higressResourceJson | ConvertFrom-Json
    $keyAuthConsumers = @($higressResource.spec.defaultConfig.consumers)
    if ($keyAuthConsumers | Where-Object { $_.name -eq $AppId }) { throw "Higress Consumer already exists: $AppId" }

    $liteInfo = Invoke-LiteLLMEnsure @{ app_id = $AppId; key = $keyL; models = @($Models); rpm_limit = $RpmLimit; tpm_limit = $TpmLimit; max_parallel_requests = $MaxParallelRequests; changed_by = $ChangedBy; create_if_missing = $true }
    $liteParsed = $liteInfo | ConvertFrom-Json
    $liteLLMKeyCreated = ($liteParsed.state -eq 'created')
    $safeInfo = $liteParsed.info
    if ($safeInfo.key_alias -ne $AppId -or (@($safeInfo.models) -join ',') -ne (@($Models) -join ',') -or [int]$safeInfo.rpm_limit -ne $RpmLimit -or [int]$safeInfo.tpm_limit -ne $TpmLimit -or [int]$safeInfo.max_parallel_requests -ne $MaxParallelRequests) {
        throw 'LiteLLM returned a Virtual Key with a mismatched declaration.'
    }

    $consumer = [ordered]@{ name = $AppId; keys = @('Authorization'); credentials = @('Bearer ' + $keyH); in_header = $true; in_query = $false }
    $higressResource.spec.defaultConfig.consumers = @($keyAuthConsumers + [pscustomobject]$consumer)
    $allow = @($higressResource.spec.matchRules[0].config.allow)
    if ($allow -notcontains $AppId) { $allow += $AppId }
    $higressResource.spec.matchRules[0].config.allow = $allow
    $higressResource.metadata.resourceVersion = $higressResource.metadata.resourceVersion
    Put-HigressResource ($higressResource | ConvertTo-Json -Depth 100 -Compress)
    $higressApplied = $true
    # key-auth keeps a live plugin cache; restart is required before the new
    # Consumer credential can be used by the verification probe.
    Restart-HigressAndWait

    $resourceTemplate = $originalResourcesText | ConvertFrom-Json
    $templateKeyAuth = $resourceTemplate | Where-Object { $_.kind -eq 'WasmPlugin' -and $_.metadata.name -eq 'p1-key-auth' }
    $templateConsumers = @($templateKeyAuth.spec.defaultConfig.consumers)
    $templateConsumer = [ordered]@{ name = $AppId; keys = @('Authorization'); credentials = @('Bearer ${' + $envName + '}'); in_header = $true; in_query = $false }
    $templateKeyAuth.spec.defaultConfig.consumers = @($templateConsumers + [pscustomobject]$templateConsumer)
    $templateAllow = @($templateKeyAuth.spec.matchRules[0].config.allow)
    if ($templateAllow -notcontains $AppId) { $templateAllow += $AppId }
    $templateKeyAuth.spec.matchRules[0].config.allow = $templateAllow
    Set-Content -LiteralPath $resourceTemplateFile -Value ($resourceTemplate | ConvertTo-Json -Depth 100) -Encoding utf8
    $resourceTemplateCommitted = $true

    $appsJson.applications = @($applications + [pscustomobject][ordered]@{
        app_id = $AppId
        display_name = $DisplayName
        higress_key_env = $envName
        models = @($Models)
        rpm_limit = $RpmLimit
        tpm_limit = $TpmLimit
        max_parallel_requests = $MaxParallelRequests
    })
    Set-Content -LiteralPath $appsFile -Value ($appsJson | ConvertTo-Json -Depth 20) -Encoding utf8
    $appsCommitted = $true

    Add-Content -LiteralPath $higressEnvFile -Value ($envName + '=' + $keyH) -Encoding ascii
    $envCommitted = $true
    Restart-MapperAndWait
    Start-Sleep -Seconds 3

    $authorizedStatus = Invoke-VerifiedStatus $keyH $Models[0] 200
    $deniedStatus = Invoke-VerifiedStatus $keyH 'p1-not-authorized-model' 403
    if ($authorizedStatus -ne 200 -or $deniedStatus -ne 403) { throw "Application verification failed: authorized=$authorizedStatus denied=$deniedStatus" }

    Set-Content -LiteralPath $keyOutputPath -Value $keyH -Encoding ascii
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $keyFingerprint = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::ASCII.GetBytes($keyH))).Replace('-', '').ToLowerInvariant()) }
    finally { $sha.Dispose() }
    $reportPath = Join-Path $reportDir ('p1-application-create-' + $AppId + '-' + $transactionId + '.md')
    @(
        '# P1 application creation report',
        '',
        ('app_id: ' + $AppId),
        ('display_name: ' + $DisplayName),
        ('models: ' + (@($Models) -join ',')),
        ('rpm_limit: ' + $RpmLimit),
        ('tpm_limit: ' + $TpmLimit),
        ('max_parallel_requests: ' + $MaxParallelRequests),
        ('authorized_status: ' + $authorizedStatus),
        ('unauthorized_model_status: ' + $deniedStatus),
        ('key_h_sha256: ' + $keyFingerprint),
        ('key_h_delivery_file: ' + $keyOutputPath),
        'key_h/key_l/master/provider key plaintext was not written to this report.'
    ) | Set-Content -LiteralPath $reportPath -Encoding utf8

    if ($RollbackAfterVerify) {
        throw 'RollbackAfterVerify requested after successful verification.'
    }
    Write-Host "P1 application created: $AppId"
    Write-Host "New Key-H written to ignored local path: $keyOutputPath"
    Write-Host "Key-H SHA256: $keyFingerprint"
    Write-Host "Report: $reportPath"
} catch {
    $failure = $_.Exception.Message
    Write-Warning "P1 application transaction failed: $failure"
    $rollbackErrors = [System.Collections.Generic.List[string]]::new()
    $rollbackStep = {
        param([string]$Name, [scriptblock]$Action)
        try { & $Action } catch { $rollbackErrors.Add($Name + ': ' + $_.Exception.Message) }
    }
    if ($appsCommitted) { & $rollbackStep 'restore apps.json' { Set-Content -LiteralPath $appsFile -Value $originalAppsText -Encoding utf8; Restart-MapperAndWait } }
    if ($resourceTemplateCommitted) { & $rollbackStep 'restore Higress template' { Set-Content -LiteralPath $resourceTemplateFile -Value $originalResourcesText -Encoding utf8 } }
    if ($envCommitted) { & $rollbackStep 'restore Higress env' { Set-Content -LiteralPath $higressEnvFile -Value $originalHigressEnvText -Encoding ascii } }
    if ($higressApplied -and $higressResourceJson) { & $rollbackStep 'restore live Higress resource' { Put-HigressResource $higressResourceJson; Restart-HigressAndWait } }
    if ($liteLLMKeyCreated) { & $rollbackStep 'block LiteLLM Key-L' { Invoke-LiteLLMBlock $keyL $ChangedBy } }
    if ($rollbackErrors.Count -eq 0) { Write-Host 'P1 application rollback completed.' }
    else { Write-Error ('P1 application rollback failed: ' + ($rollbackErrors -join '; ')) }
    if ($RollbackAfterVerify -and $failure -eq 'RollbackAfterVerify requested after successful verification.') { exit 0 }
    if ($rollbackErrors.Count -gt 0) { exit 1 }
    exit 1
} finally {
    if ($lock) { $lock.Dispose() }
}

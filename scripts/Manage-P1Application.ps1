[CmdletBinding()]
param(
    [ValidateSet('Get', 'SetPolicy', 'Disable', 'Enable', 'Reconcile', 'RotateKeyH', 'RetireKeyH')]
    [string]$Action = 'Get',
    [string]$AppId,
    [string[]]$Models,
    [string]$ModelsB64,
    [int]$RpmLimit,
    [int]$TpmLimit,
    [int]$MaxParallelRequests,
    [string]$ChangedBy = 'p1-local-admin',
    [switch]$RestartHigress
)

$ErrorActionPreference = 'Stop'
$Models = if ($ModelsB64) { @([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($ModelsB64)) | ConvertFrom-Json) } else { @($Models) }
$Models = @($Models | ForEach-Object { $value = ([string]$_).Trim('"'); if ($value.Contains(',')) { $value.Split(',') } else { $value } } | ForEach-Object { ([string]$_).Trim('"').Trim() } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root 'deploy/docker-compose.p1-test.yml'
$envFile = Join-Path $root '.env.p1-test'
$appsFile = Join-Path $root 'config/p1-test/apps.json'
$secretFile = Join-Path $root 'runtime/p1-test/secrets/p1_key_derivation_secret'
$higressEnvFile = Join-Path $root '.env.p1-higress-test'
$rotationDir = Join-Path $root 'runtime/p1-test/rotations'

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

function Get-HigressKeyEnvName([string]$Id) {
    $applications = @((Get-Content -LiteralPath $appsFile -Raw -Encoding utf8 | ConvertFrom-Json).applications)
    for ($index = 0; $index -lt $applications.Count; $index++) {
        if ($applications[$index].app_id -eq $Id) {
            if (-not [string]::IsNullOrWhiteSpace($applications[$index].higress_key_env)) { return [string]$applications[$index].higress_key_env }
            $suffix = ($Id.ToUpperInvariant() -replace '[^A-Z0-9]', '_')
            return ('P1_HIGRESS_KEY_' + $suffix + '_KEY')
        }
    }
    throw "Cannot map app_id to a P1 Higress key variable: $Id"
}

function Get-EnvValue([string]$Path, [string]$Name) {
    $line = Get-Content -LiteralPath $Path -Encoding ascii | Where-Object { $_ -like ($Name + '=*') } | Select-Object -First 1
    if (-not $line) { return $null }
    return $line.Substring($Name.Length + 1)
}

function Set-EnvValue([string]$Path, [string]$Name, [string]$Value) {
    $lines = @(Get-Content -LiteralPath $Path -Encoding ascii)
    $found = $false
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -like ($Name + '=*')) { $lines[$index] = $Name + '=' + $Value; $found = $true }
    }
    if (-not $found) { throw "Missing $Name in $Path" }
    Set-Content -LiteralPath $Path -Value $lines -Encoding ascii
}

function Restart-HigressAndWait {
    docker compose --env-file $envFile -f $composeFile restart higress | Out-Null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $health = docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}' api-gateway-p1-test-higress-1 2>$null
        if ($health -eq 'healthy') {
            try {
                Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri 'http://127.0.0.1:28080/v1/models' -Headers @{ Host = 'ai-gateway-p1-test.local' } -ErrorAction Stop | Out-Null
                return
            } catch {
                if ($_.Exception.Response) { return }
            }
        }
        Start-Sleep -Seconds 3
    }
    throw 'P1 Higress did not become healthy in 90 seconds.'
}

function Invoke-LiteLLMAdmin([hashtable]$Payload) {
    $json = $Payload | ConvertTo-Json -Compress -Depth 8
    $env:P1_ADMIN_B64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
    $python = @'
import base64, json, os, urllib.parse, urllib.request
p = json.loads(base64.b64decode(os.environ["P1_ADMIN_B64"]))
key = p.pop("_key")
action = p.pop("_action")
headers = {"Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"], "Content-Type": "application/json", "litellm-changed-by": p.pop("_changed_by", "p1-local-admin")}
base = "http://127.0.0.1:4000"
if action in ("Get", "Reconcile"):
    url = base + "/key/info?" + urllib.parse.urlencode({"key": key})
    request = urllib.request.Request(url, method="GET", headers=headers)
else:
    endpoint = {"SetPolicy": "/key/update", "Disable": "/key/block", "Enable": "/key/unblock"}[action]
    if action in ("Disable", "Enable"):
        body = {"key": key}
    else:
        body = {"key": key}
        body.update(p)
    request = urllib.request.Request(base + endpoint, data=json.dumps(body).encode(), method="POST", headers=headers)
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read() or b"{}")
        if action in ("Get", "Reconcile"):
            info = result.get("info", {})
            safe = {name: info.get(name) for name in ("key_alias", "models", "rpm_limit", "tpm_limit", "max_parallel_requests", "blocked", "expires", "metadata")}
            print(json.dumps(safe, ensure_ascii=False, sort_keys=True))
        else:
            print(action + " status=" + str(response.status))
except Exception as error:
    raise SystemExit("LiteLLM admin operation failed: " + type(error).__name__)
'@
    try {
        $output = $python | docker compose --env-file $envFile -f $composeFile exec -T -e P1_ADMIN_B64 litellm python -
        if ($LASTEXITCODE -ne 0) { throw 'LiteLLM admin operation failed.' }
        return ($output -join "`n")
    } finally { Remove-Item Env:P1_ADMIN_B64 -ErrorAction SilentlyContinue }
}

function Set-HigressConsumerAllowed([string]$Id, [bool]$Allowed) {
    $payload = @{ app_id = $Id; allowed = $Allowed } | ConvertTo-Json -Compress
    $env:P1_HIGRESS_ADMIN_B64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payload))
    $python = @'
import base64, json, os, ssl, urllib.request
p = json.loads(base64.b64decode(os.environ["P1_HIGRESS_ADMIN_B64"]))
base = "https://127.0.0.1:18443/apis/extensions.higress.io/v1alpha1/namespaces/higress-system/wasmplugins/p1-key-auth"
ctx = ssl._create_unverified_context()
with urllib.request.urlopen(urllib.request.Request(base), context=ctx) as response:
    resource = json.load(response)
rule = resource["spec"]["matchRules"][0]["config"]["allow"]
if p["allowed"] and p["app_id"] not in rule:
    rule.append(p["app_id"])
if not p["allowed"]:
    rule[:] = [item for item in rule if item != p["app_id"]]
resource["metadata"]["resourceVersion"] = resource.get("metadata", {}).get("resourceVersion")
request = urllib.request.Request(base, data=json.dumps(resource, separators=(",", ":")).encode(), method="PUT", headers={"Content-Type": "application/json"})
with urllib.request.urlopen(request, context=ctx) as response:
    if response.status not in (200, 201):
        raise SystemExit("unexpected Higress status")
print("Higress allow=" + str(p["allowed"]).lower())
'@
    try {
        $output = $python | docker compose --env-file $envFile -f $composeFile exec -T -e P1_HIGRESS_ADMIN_B64 higress python3 -
        if ($LASTEXITCODE -ne 0) { throw 'Higress Consumer update failed.' }
        Write-Host ($output -join "`n")
    } finally { Remove-Item Env:P1_HIGRESS_ADMIN_B64 -ErrorAction SilentlyContinue }
}

function Update-HigressConsumerCredentials([string]$Id, [string]$NewKey, [bool]$RetireOld) {
    $payload = @{ app_id = $Id; new_key = $NewKey; retire_old = $RetireOld } | ConvertTo-Json -Compress
    $env:P1_HIGRESS_CREDENTIALS_B64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payload))
    $python = @'
import base64, json, os, ssl, urllib.request
p = json.loads(base64.b64decode(os.environ["P1_HIGRESS_CREDENTIALS_B64"]))
base = "https://127.0.0.1:18443/apis/extensions.higress.io/v1alpha1/namespaces/higress-system/wasmplugins/p1-key-auth"
ctx = ssl._create_unverified_context()
with urllib.request.urlopen(urllib.request.Request(base), context=ctx) as response:
    resource = json.load(response)
consumers = resource["spec"]["defaultConfig"]["consumers"]
consumer = next((item for item in consumers if item.get("name") == p["app_id"]), None)
if consumer is None:
    raise SystemExit("Higress Consumer not found")
new_credential = "Bearer " + p["new_key"]
credentials = list(consumer.get("credentials", []))
if p["retire_old"]:
    credentials = [new_credential]
elif new_credential not in credentials:
    credentials.append(new_credential)
consumer["credentials"] = credentials
resource["metadata"]["resourceVersion"] = resource.get("metadata", {}).get("resourceVersion")
request = urllib.request.Request(base, data=json.dumps(resource, separators=(",", ":")).encode(), method="PUT", headers={"Content-Type":"application/json"})
with urllib.request.urlopen(request, context=ctx) as response:
    if response.status not in (200, 201):
        raise SystemExit("unexpected Higress status")
print("Higress credentials=" + str(len(credentials)))
'@
    try {
        $output = $python | docker compose --env-file $envFile -f $composeFile exec -T -e P1_HIGRESS_CREDENTIALS_B64 higress python3 -
        if ($LASTEXITCODE -ne 0) { throw 'Higress Consumer credential update failed.' }
        Write-Host ($output -join "`n")
    } finally { Remove-Item Env:P1_HIGRESS_CREDENTIALS_B64 -ErrorAction SilentlyContinue }
}

$applications = (Get-Content -LiteralPath $appsFile -Raw -Encoding utf8 | ConvertFrom-Json).applications
if ([string]::IsNullOrWhiteSpace($AppId)) {
    if ($Action -eq 'Get') {
        $applications | ForEach-Object { Write-Host "$($_.app_id) $($_.display_name)" }
        exit 0
    }
    throw '-AppId is required for this action.'
}
$app = $applications | Where-Object { $_.app_id -eq $AppId }
if (-not $app) { throw "Unknown P1 app_id: $AppId" }
$secret = [IO.File]::ReadAllBytes($secretFile)
$key = Get-DerivedKey $secret $AppId

switch ($Action) {
    'Get' {
        Invoke-LiteLLMAdmin @{ _action = 'Get'; _key = $key; _changed_by = $ChangedBy }
    }
    'Reconcile' {
        $infoJson = Invoke-LiteLLMAdmin @{ _action = 'Reconcile'; _key = $key; _changed_by = $ChangedBy }
        Write-Host $infoJson
        $info = $infoJson | ConvertFrom-Json
        $actualModels = @($info.models | ForEach-Object { [string]$_ } | Sort-Object)
        $expectedModels = @($app.models | ForEach-Object { [string]$_ } | Sort-Object)
        $modelsMatch = (($actualModels -join ',') -eq ($expectedModels -join ','))
        $policyMatch = ($info.rpm_limit -eq [int]$app.rpm_limit -and
            $info.tpm_limit -eq [int]$app.tpm_limit -and
            $info.max_parallel_requests -eq [int]$app.max_parallel_requests)
        if ($info.key_alias -ne $AppId -or -not $modelsMatch -or -not $policyMatch) {
            throw "P1 application reconciliation failed for $AppId"
        }
        Write-Host 'Reconcile PASS'
    }
    'SetPolicy' {
        if (-not $Models -or $Models.Count -eq 0) { throw '-Models is required for SetPolicy.' }
        if ($RpmLimit -le 0 -or $TpmLimit -le 0 -or $MaxParallelRequests -le 0) { throw 'RPM, TPM and max parallel requests must be positive.' }
        $result = Invoke-LiteLLMAdmin @{ _action = 'SetPolicy'; _key = $key; _changed_by = $ChangedBy; models = @($Models); rpm_limit = $RpmLimit; tpm_limit = $TpmLimit; max_parallel_requests = $MaxParallelRequests; metadata = @{ app_id = $AppId; p1 = 'application-policy' } }
        Write-Host $result
        # Keep the local desired-state manifest aligned with LiteLLM after a
        # successful policy update. This is required by the organization
        # model-policy guard, which must not silently revoke a model that an
        # application still declares as allowed.
        $document = Get-Content -LiteralPath $appsFile -Raw -Encoding utf8 | ConvertFrom-Json
        $manifestApp = @($document.applications) | Where-Object { $_.app_id -eq $AppId } | Select-Object -First 1
        if (-not $manifestApp) { throw "Unknown P1 app_id: $AppId" }
        $manifestApp.models = @($Models)
        $manifestApp.rpm_limit = $RpmLimit
        $manifestApp.tpm_limit = $TpmLimit
        $manifestApp.max_parallel_requests = $MaxParallelRequests
        $temporaryAppsFile = $appsFile + '.tmp'
        $document | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporaryAppsFile -Encoding utf8
        Move-Item -LiteralPath $temporaryAppsFile -Destination $appsFile -Force
        Write-Host "P1 application manifest updated for $AppId."
    }
    'Disable' {
        Set-HigressConsumerAllowed $AppId $false
        $result = Invoke-LiteLLMAdmin @{ _action = 'Disable'; _key = $key; _changed_by = $ChangedBy }
        Write-Host $result
    }
    'Enable' {
        $result = Invoke-LiteLLMAdmin @{ _action = 'Enable'; _key = $key; _changed_by = $ChangedBy }
        Write-Host $result
        Set-HigressConsumerAllowed $AppId $true
    }
    'RotateKeyH' {
        if (-not (Test-Path -LiteralPath $higressEnvFile)) { throw "Missing $higressEnvFile" }
        $envName = Get-HigressKeyEnvName $AppId
        $oldKey = Get-EnvValue $higressEnvFile $envName
        if ([string]::IsNullOrWhiteSpace($oldKey)) { throw "Missing current Key-H for $AppId" }
        $newKey = New-KeyH
        Update-HigressConsumerCredentials $AppId $newKey $false
        Set-EnvValue $higressEnvFile $envName $newKey
        New-Item -ItemType Directory -Force -Path $rotationDir | Out-Null
        $outputPath = Join-Path $rotationDir ($AppId + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.key')
        Set-Content -LiteralPath $outputPath -Value $newKey -Encoding ascii
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $fingerprint = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::ASCII.GetBytes($newKey))).Replace('-', '').ToLowerInvariant()) }
        finally { $sha.Dispose() }
        Write-Host "Key-H rotation added a dual credential for $AppId."
        Write-Host "New Key-H written to ignored local path: $outputPath"
        Write-Host "New Key-H SHA256: $fingerprint"
        Write-Host 'Run RetireKeyH after the client migration window to remove the old credential.'
    }
    'RetireKeyH' {
        if (-not $RestartHigress) { throw 'Higress keeps removed credentials in the live plugin cache; rerun RetireKeyH with -RestartHigress.' }
        if (-not (Test-Path -LiteralPath $higressEnvFile)) { throw "Missing $higressEnvFile" }
        $envName = Get-HigressKeyEnvName $AppId
        $currentKey = Get-EnvValue $higressEnvFile $envName
        if ([string]::IsNullOrWhiteSpace($currentKey)) { throw "Missing current Key-H for $AppId" }
        Update-HigressConsumerCredentials $AppId $currentKey $true
        Restart-HigressAndWait
        Write-Host "Retired old Key-H credentials for $AppId; current credential remains active."
    }
}

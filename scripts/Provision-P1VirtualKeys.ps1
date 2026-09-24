[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root 'deploy/docker-compose.p1-test.yml'
$envFile = Join-Path $root '.env.p1-test'
$secretFile = Join-Path $root 'runtime/p1-test/secrets/p1_key_derivation_secret'
$appsFile = Join-Path $root 'config/p1-test/apps.json'

function Get-DerivedKey([byte[]]$Secret, [string]$AppId) {
    $hmac = [System.Security.Cryptography.HMACSHA256]::new($Secret)
    try {
        $inputBytes = [Text.Encoding]::ASCII.GetBytes('campus-ai-gateway/litellm-key/v1/' + $AppId)
        $digest = $hmac.ComputeHash($inputBytes)
    } finally {
        $hmac.Dispose()
    }
    return 'sk-' + [Convert]::ToBase64String($digest).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

if (-not (Test-Path -LiteralPath $envFile)) { throw "Missing $envFile. Run Initialize-P1TestEnvironment.ps1 first." }
if (-not (Test-Path -LiteralPath $secretFile)) { throw "Missing P1 derivation secret." }
$secret = [IO.File]::ReadAllBytes($secretFile)
$apps = (Get-Content -LiteralPath $appsFile -Raw -Encoding utf8 | ConvertFrom-Json).applications

$python = @'
import base64, hashlib, json, os, urllib.request
payload = json.loads(base64.b64decode(os.environ["P1_PROVISION_B64"]))
headers = {"Content-Type":"application/json", "Authorization":"Bearer " + os.environ["LITELLM_MASTER_KEY"]}
list_request = urllib.request.Request("http://127.0.0.1:4000/key/list", method="GET", headers=headers)
try:
    with urllib.request.urlopen(list_request, timeout=15) as response:
        existing = json.load(response).get("keys", [])
    expected_hash = hashlib.sha256(payload["key"].encode()).hexdigest()
    if expected_hash in existing:
        update = {name: payload[name] for name in ("key", "models", "rpm_limit", "tpm_limit", "max_parallel_requests", "metadata")}
        request = urllib.request.Request("http://127.0.0.1:4000/key/update", data=json.dumps(update).encode(), method="POST", headers=headers)
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status not in (200, 201):
                raise RuntimeError("unexpected update status")
        print("updated " + payload["key_alias"])
    else:
        request = urllib.request.Request("http://127.0.0.1:4000/key/generate", data=json.dumps(payload).encode(), method="POST", headers=headers)
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status not in (200, 201):
                raise RuntimeError("unexpected status")
        print("provisioned " + payload["key_alias"])
except Exception as error:
    raise SystemExit("LiteLLM key provisioning failed: " + type(error).__name__)
'@

foreach ($app in $apps) {
    $key = Get-DerivedKey $secret $app.app_id
    $payload = [ordered]@{
        key = $key
        key_alias = $app.app_id
        key_type = 'llm_api'
        models = @($app.models)
        rpm_limit = [int]$app.rpm_limit
        tpm_limit = [int]$app.tpm_limit
        max_parallel_requests = [int]$app.max_parallel_requests
        metadata = @{ app_id = $app.app_id; p1 = 'technical-gate' }
    } | ConvertTo-Json -Compress -Depth 5
    $env:P1_PROVISION_B64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payload))
    try {
        $python | docker compose --env-file $envFile -f $composeFile exec -T -e P1_PROVISION_B64 litellm python -
        if ($LASTEXITCODE -ne 0) { throw "LiteLLM rejected $($app.app_id)" }
    } finally {
        Remove-Item Env:P1_PROVISION_B64 -ErrorAction SilentlyContinue
    }
}

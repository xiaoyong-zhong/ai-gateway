[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SnapshotPath,
    [string]$ChangedBy = 'p1-local-admin'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root 'deploy/docker-compose.p1-test.yml'
$envFile = Join-Path $root '.env.p1-test'
$resolved = (Resolve-Path -LiteralPath $SnapshotPath).Path
$runtimeRoot = (Resolve-Path (Join-Path $root 'runtime/p1-test/higress/snapshots')).Path
if (-not $resolved.StartsWith($runtimeRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Rollback is limited to an ignored local runtime/p1-test/higress/snapshots path.'
}
if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) { throw "Snapshot file not found: $resolved" }
$snapshotText = Get-Content -LiteralPath $resolved -Raw -Encoding utf8
$snapshot = $snapshotText | ConvertFrom-Json

$env:P1_HIGRESS_SNAPSHOT_B64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($snapshotText))
$python = @'
import base64, json, os, ssl, urllib.request
from urllib.error import HTTPError

snapshot = json.loads(base64.b64decode(os.environ['P1_HIGRESS_SNAPSHOT_B64']))
base = 'https://127.0.0.1:18443'
paths = {
    'McpBridge': '/apis/networking.higress.io/v1/namespaces/higress-system/mcpbridges/',
    'Ingress': '/apis/networking.k8s.io/v1/namespaces/higress-system/ingresses/',
    'WasmPlugin': '/apis/extensions.higress.io/v1alpha1/namespaces/higress-system/wasmplugins/',
}
ctx = ssl._create_unverified_context()
def request(method, url, payload=None):
    data = None if payload is None else json.dumps(payload, separators=(',', ':')).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None: req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, context=ctx, timeout=15) as response:
        return response.status, json.loads(response.read() or b'{}')

for key, desired in snapshot.items():
    kind = desired['kind']; name = desired['metadata']['name']
    url = base + paths[kind] + name
    try:
        _, current = request('GET', url)
        desired['metadata']['resourceVersion'] = current.get('metadata', {}).get('resourceVersion')
        request('PUT', url, desired)
    except HTTPError as error:
        if error.code != 409: raise
        _, current = request('GET', url)
        desired['metadata']['resourceVersion'] = current.get('metadata', {}).get('resourceVersion')
        request('PUT', url, desired)
    print('restored ' + kind + '/' + name)
'@
try {
    $output = $python | docker compose --env-file $envFile -f $composeFile exec -T -e P1_HIGRESS_SNAPSHOT_B64 higress python3 -
    if ($LASTEXITCODE -ne 0) { throw 'Higress snapshot restore failed.' }
    Write-Host ($output -join "`n")
} finally { Remove-Item Env:P1_HIGRESS_SNAPSHOT_B64 -ErrorAction SilentlyContinue }

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
    if ($attempt -eq 29) { throw 'P1 Higress did not become ready after rollback.' }
}

Write-Host "P1 Higress rollback completed by $ChangedBy."

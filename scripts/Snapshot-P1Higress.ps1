[CmdletBinding()]
param(
    [string]$SnapshotId = (Get-Date -Format 'yyyyMMdd-HHmmss')
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root 'deploy/docker-compose.p1-test.yml'
$envFile = Join-Path $root '.env.p1-test'
$snapshotRoot = Join-Path $root 'runtime/p1-test/higress/snapshots'
$snapshotDir = Join-Path $snapshotRoot $SnapshotId
$rawFile = Join-Path $snapshotDir 'resources.json'
$summaryFile = Join-Path $snapshotDir 'summary.md'

if ($SnapshotId -notmatch '^[0-9]{8}-[0-9]{6}(-[A-Za-z0-9_-]+)?$') {
    throw 'SnapshotId must be a timestamp-like local identifier.'
}
New-Item -ItemType Directory -Force -Path $snapshotDir | Out-Null

$python = @'
import json, ssl, urllib.request

resources = json.load(open('/p1-config/resources.json', encoding='utf-8-sig'))
base = 'https://127.0.0.1:18443'
paths = {
    ('networking.higress.io/v1', 'McpBridge'): '/apis/networking.higress.io/v1/namespaces/higress-system/mcpbridges/',
    ('networking.k8s.io/v1', 'Ingress'): '/apis/networking.k8s.io/v1/namespaces/higress-system/ingresses/',
    ('extensions.higress.io/v1alpha1', 'WasmPlugin'): '/apis/extensions.higress.io/v1alpha1/namespaces/higress-system/wasmplugins/',
}
ctx = ssl._create_unverified_context()
result = {}
for item in resources:
    key = (item['apiVersion'], item['kind'])
    url = base + paths[key] + item['metadata']['name']
    with urllib.request.urlopen(urllib.request.Request(url, method='GET'), context=ctx, timeout=15) as response:
        live = json.load(response)
    result[item['kind'] + '/' + item['metadata']['name']] = live
print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
'@

$raw = $python | docker compose --env-file $envFile -f $composeFile exec -T higress python3 -
if ($LASTEXITCODE -ne 0 -or -not $raw) { throw 'Failed to snapshot live P1 Higress resources.' }
$rawText = $raw -join "`n"
$null = $rawText | ConvertFrom-Json
[IO.File]::WriteAllText($rawFile, $rawText, [Text.UTF8Encoding]::new($false))

$rawObject = $rawText | ConvertFrom-Json
$rows = @(
    '| Resource | ResourceVersion | Secret-bearing fields | SHA256 |',
    '|---|---|---|---|'
)
foreach ($property in $rawObject.psobject.Properties) {
    $resource = $property.Value
    $json = $resource | ConvertTo-Json -Depth 100 -Compress
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($json))).Replace('-', '').ToLowerInvariant()) }
    finally { $sha.Dispose() }
    $secretFields = @()
    if ($resource.spec.defaultConfig.consumers) { $secretFields += 'consumer.credentials' }
    $rows += ('| `' + $property.Name + '` | `' + [string]$resource.metadata.resourceVersion + '` | ' + ($(if ($secretFields) { $secretFields -join ', ' } else { 'none' })) + ' | `' + $digest + '` |')
}
@(
    '# P1 Higress local snapshot',
    '',
    ('snapshot_id: ' + $SnapshotId),
    ('created_at: ' + (Get-Date -Format 'o')),
    '',
    'Raw resources are stored only under ignored runtime/p1-test/higress/snapshots/.',
    'Consumer credential plaintext is not included in this summary; do not copy resources.json to Git.',
    '',
    $rows
) | Set-Content -LiteralPath $summaryFile -Encoding utf8

Write-Host "P1 Higress snapshot created: $snapshotDir"
Write-Host "Summary: $summaryFile"

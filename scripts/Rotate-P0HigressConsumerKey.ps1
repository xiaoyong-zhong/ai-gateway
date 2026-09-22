[CmdletBinding()]
param([switch]$RetireLegacy)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env.p0-test'
$composeFile = Join-Path $root 'deploy/docker-compose.p0-test.yml'
if (-not (Test-Path -LiteralPath $envFile)) { throw 'Missing .env.p0-test. Initialize the local P0 test environment first.' }

function New-RandomHex([int]$ByteCount) {
    $bytes = [byte[]]::new($ByteCount)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant())
}

function Get-ModelStatus([string]$Key) {
    try {
        $response = Invoke-WebRequest -Uri 'http://ai-gateway-test.local:18080/v1/models' -Headers @{ Authorization = "Bearer $Key"; Accept = 'application/json' } -Method Get -UseBasicParsing -TimeoutSec 20
        return [int]$response.StatusCode
    } catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        throw
    }
}

function Apply-P0AuthConfig {
    Push-Location $root
    try {
        & docker compose --env-file .env.p0-test -f $composeFile exec -T higress python3 -X utf8 /p0-scripts/bootstrap_higress_p0.py --env-file /p0-env/.env.p0-test --resource-file /p0-config/resources.json
        if ($LASTEXITCODE -ne 0) { throw "Higress bootstrap failed with exit code $LASTEXITCODE." }
    } finally { Pop-Location }
}

$originalLines = @(Get-Content -LiteralPath $envFile -Encoding ASCII)
$original = @{}
foreach ($line in $originalLines) {
    if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') { $original[$matches[1]] = $matches[2] }
}
if (-not $original['P0_HIGRESS_CONSUMER_KEY']) { throw 'P0_HIGRESS_CONSUMER_KEY is missing.' }

$snapshotOutput = & python (Join-Path $PSScriptRoot 'p0_release.py') backup --message 'pre-consumer-key-rotation'
if ($LASTEXITCODE -ne 0) { throw 'Could not create a local P0 recovery snapshot; key rotation was not started.' }
$releaseLine = @($snapshotOutput | Where-Object { $_ -match '^Created local P0 snapshot: ' } | Select-Object -First 1)
if (-not $releaseLine) { throw 'Snapshot tool did not return a recovery ID; key rotation was not started.' }
$releaseId = ($releaseLine -split ': ', 2)[1].Trim()
$snapshotEnv = Join-Path $root "runtime/p0-test/releases/$releaseId/files/.env.p0-test"
if (-not (Test-Path -LiteralPath $snapshotEnv)) { throw 'The local recovery snapshot is missing its private P0 environment file.' }

$updatedValues = @{}
foreach ($entry in $original.GetEnumerator()) { $updatedValues[$entry.Key] = $entry.Value }
if ($RetireLegacy) {
    $updatedValues['P0_HIGRESS_LEGACY_CONSUMER_KEY'] = ''
} else {
    $updatedValues['P0_HIGRESS_LEGACY_CONSUMER_KEY'] = $original['P0_HIGRESS_CONSUMER_KEY']
    $updatedValues['P0_HIGRESS_CONSUMER_KEY'] = 'gw-p0-' + (New-RandomHex 24)
}
$updatedLines = foreach ($line in $originalLines) {
    if ($line -match '^([A-Z][A-Z0-9_]*)=') {
        $name = $matches[1]
        if ($updatedValues.ContainsKey($name)) { "$name=$($updatedValues[$name])"; continue }
    }
    $line
}
$temporaryEnv = "$envFile.rotation-tmp"

try {
    [System.IO.File]::WriteAllLines($temporaryEnv, [string[]]$updatedLines, [System.Text.Encoding]::ASCII)
    Move-Item -LiteralPath $temporaryEnv -Destination $envFile -Force
    Apply-P0AuthConfig

    $newStatus = Get-ModelStatus $updatedValues['P0_HIGRESS_CONSUMER_KEY']
    if ($newStatus -ne 200) { throw "New/current Consumer Key did not authenticate (HTTP $newStatus)." }
    if ($RetireLegacy) {
        if ($original['P0_HIGRESS_LEGACY_CONSUMER_KEY']) {
            $retiredStatus = Get-ModelStatus $original['P0_HIGRESS_LEGACY_CONSUMER_KEY']
            if ($retiredStatus -ne 401) { throw "Retired legacy Consumer Key should return 401 (HTTP $retiredStatus)." }
        }
    } elseif ($updatedValues['P0_HIGRESS_LEGACY_CONSUMER_KEY']) {
        $legacyStatus = Get-ModelStatus $updatedValues['P0_HIGRESS_LEGACY_CONSUMER_KEY']
        if ($legacyStatus -ne 200) { throw "Compatibility Consumer Key did not authenticate (HTTP $legacyStatus)." }
    }
} catch {
    $rotationError = $_.Exception.Message
    try {
        Copy-Item -LiteralPath $snapshotEnv -Destination $envFile -Force
        Apply-P0AuthConfig
        $restoredStatus = Get-ModelStatus $original['P0_HIGRESS_CONSUMER_KEY']
        if ($restoredStatus -ne 200) { throw "restored Consumer Key returned HTTP $restoredStatus" }
    } catch {
        throw "Key operation failed ($rotationError); automatic local recovery also failed ($($_.Exception.Message)). Do not continue until runtime and .env are reconciled."
    }
    throw "Key operation failed and the previous local key/configuration was restored: $rotationError"
} finally {
    if (Test-Path -LiteralPath $temporaryEnv) { Remove-Item -LiteralPath $temporaryEnv -Force }
}

if ($RetireLegacy) {
    Write-Output 'Legacy Consumer Key retired and verified as rejected. The active key value was not printed.'
} else {
    Write-Output 'New Consumer Key and immediately previous compatibility key both returned HTTP 200.'
    Write-Output 'Update CCSwitch from the local .env.p0-test value. The key value was not printed.'
}
Write-Output "Recovery snapshot: $releaseId (local Git-ignored runtime/p0-test/releases only)."

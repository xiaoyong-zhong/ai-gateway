# P0 配置回滚脚本
# 用于将 Higress 配置恢复到指定版本
#
# 用法:
#   # 查看可用版本:
#   powershell -ExecutionPolicy Bypass -File scripts/Rollback-P0HigressConfig.ps1 -ListVersions
#
#   # 回滚到指定版本:
#   powershell -ExecutionPolicy Bypass -File scripts/Rollback-P0HigressConfig.ps1 -Version "1.0.0"
#
#   # 回滚到上一个版本:
#   powershell -ExecutionPolicy Bypass -File scripts/Rollback-P0HigressConfig.ps1 -PreviousVersion

[CmdletBinding()]
param(
    [switch]$ListVersions,
    [string]$Version,
    [switch]$PreviousVersion
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$backupDir = Join-Path $root 'config\higress\p0-test\backups'
$envFile = Join-Path $root '.env.p0-test'
$composeFile = Join-Path $root 'deploy\docker-compose.p0-test.yml'

function Format-Timestamp {
    param([string]$Timestamp)
    return (Get-Date $Timestamp).ToString("yyyy-MM-dd HH:mm:ss")
}

# List available versions
if ($ListVersions -or (-not $Version -and -not $PreviousVersion)) {
    Write-Host "=== P0 Higress 配置版本列表 ==="
    Write-Host ""

    if (-not (Test-Path -LiteralPath $backupDir)) {
        Write-Host "No backups found. Create backups before deploying changes:"
        Write-Host "  powershell -File scripts/Backup-P0Config.ps1"
        exit 0
    }

    $backups = Get-ChildItem -LiteralPath $backupDir -Filter "resources-*.json" |
        Sort-Object Name

    if ($backups.Count -eq 0) {
        Write-Host "No backups found."
        exit 0
    }

    Write-Host "Available versions:"
    Write-Host ""
    $i = 0
    foreach ($backup in $backups) {
        $i++
        $version = $backup.Name -replace 'resources-(.+)\.json', '$1'
        $timestamp = Format-Timestamp -Timestamp ($backup.LastWriteTime.ToString("o"))
        Write-Host "  [$i] $version  (created: $timestamp, size: $($backup.Length) bytes)"
    }
    Write-Host ""
    Write-Host "Usage: powershell -File $PSScriptRoot\Rollback-P0HigressConfig.ps1 -Version <version>"
    Write-Host "       powershell -File $PSScriptRoot\Rollback-P0HigressConfig.ps1 -PreviousVersion"
    exit 0
}

# Determine target version
$targetFile = $null

if ($PreviousVersion) {
    $backups = Get-ChildItem -LiteralPath $backupDir -Filter "resources-*.json" |
        Sort-Object Name
    if ($backups.Count -lt 2) {
        throw 'No previous version available (need at least 2 backups).'
    }
    $targetFile = $backups[-2].FullName  # Second to last
    $Version = (Split-Path $targetFile -Leaf) -replace 'resources-(.+)\.json', '$1'
}

if ($Version) {
    $pattern = "resources-$Version.json"
    $targetFile = Join-Path $backupDir $pattern
    if (-not (Test-Path -LiteralPath $targetFile)) {
        throw "Version $Version not found in $backupDir"
    }
}

if (-not $targetFile) {
    throw 'Specify -Version or -PreviousVersion.'
}

Write-Host "=== P0 Higress 配置回滚 ==="
Write-Host "Target version: $Version"
Write-Host "Source file: $targetFile"
Write-Host ""

# Verify target file is valid JSON
try {
    Get-Content -LiteralPath $targetFile -Raw | ConvertFrom-Json | Out-Null
    Write-Host "PASS: Target configuration is valid JSON"
} catch {
    throw "Target configuration is not valid JSON: $($_.Exception.Message)"
}

# Backup current state before rollback
$currentResources = Join-Path $root 'config\higress\p0-test\resources.json'
if (Test-Path -LiteralPath $currentResources) {
    $preRollbackBackup = Join-Path $backupDir "resources-pre-rollback-$(Get-Date -Format 'yyyyMMdd-HHmmss').json"
    Copy-Item -LiteralPath $currentResources -Destination $preRollbackBackup -Force
    Write-Host "Backed up current config to: $preRollbackBackup"
}

# Copy backup to current location
Copy-Item -LiteralPath $targetFile -Destination $currentResources -Force
Write-Host "Restored resources.json to version $Version"

# Re-apply to Higress
if (-not (Test-Path -LiteralPath $envFile)) {
    throw 'Missing .env.p0-test. Run scripts/Initialize-P0TestEnvironment.ps1 first.'
}

Write-Host ""
Write-Host "Re-applying configuration to Higress..."

Push-Location $root
try {
    docker compose --env-file $envFile -f $composeFile exec -T higress python3 -X utf8 `
        /p0-scripts/bootstrap_higress_p0.py `
        --env-file /p0-env/.env.p0-test `
        --resource-file /p0-config/resources.json
    Write-Host ""
    Write-Host "PASS: Configuration rollback to $Version complete."
    Write-Host ""
    Write-Host "Verify with: powershell -File scripts/Test-P0Gateway.ps1"
} finally {
    Pop-Location
}
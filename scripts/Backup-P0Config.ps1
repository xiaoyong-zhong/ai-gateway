# P0 配置备份脚本
# 在修改 Higress 配置前执行备份，用于后续回滚
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts/Backup-P0Config.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/Backup-P0Config.ps1 -Message "Added IP restriction"

[CmdletBinding()]
param(
    [string]$Message = "manual-backup"
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$resourcesFile = Join-Path $root 'config\higress\p0-test\resources.json'
$ipRestrictionFile = Join-Path $root 'config\higress\p0-test\ip-restriction.json'
$backupDir = Join-Path $root 'config\higress\p0-test\backups'

# Create backup directory
if (-not (Test-Path -LiteralPath $backupDir)) {
    New-Item -Path $backupDir -ItemType Directory -Force | Out-Null
}

# Generate version string from timestamp
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$version = "$timestamp"

# Sanitize message for filename use (replace special chars with dashes)
$safeMessage = $Message -replace '[^\w\s-]', '' -replace '\s+', '-'
$safeMessage = $safeMessage.Substring(0, [Math]::Min(50, $safeMessage.Length))

# Backup resources.json
if (Test-Path -LiteralPath $resourcesFile) {
    $backupName = "resources-$version-$safeMessage.json"
    $backupPath = Join-Path $backupDir $backupName
    Copy-Item -LiteralPath $resourcesFile -Destination $backupPath -Force
    Write-Host "Backed up resources.json -> $backupName"
} else {
    Write-Host "SKIP: resources.json not found"
}

# Backup ip-restriction.json if it exists
if (Test-Path -LiteralPath $ipRestrictionFile) {
    $ipBackupName = "ip-restriction-$version-$safeMessage.json"
    $ipBackupPath = Join-Path $backupDir $ipBackupName
    Copy-Item -LiteralPath $ipRestrictionFile -Destination $ipBackupPath -Force
    Write-Host "Backed up ip-restriction.json -> $ipBackupName"
}

# Backup litellm config
$litellmConfig = Join-Path $root 'config\litellm.yaml'
if (Test-Path -LiteralPath $litellmConfig) {
    $litellmBackupName = "litellm-$version-$safeMessage.yaml"
    $litellmBackupPath = Join-Path $backupDir $litellmBackupName
    Copy-Item -LiteralPath $litellmConfig -Destination $litellmBackupPath -Force
    Write-Host "Backed up litellm.yaml -> $litellmBackupName"
}

# Backup .env.p0-test (without secrets in version control)
$envFile = Join-Path $root '.env.p0-test'
if (Test-Path -LiteralPath $envFile) {
    $envBackupName = ".env.p0-test-$version-$safeMessage"
    $envBackupPath = Join-Path $backupDir $envBackupName
    Copy-Item -LiteralPath $envFile -Destination $envBackupPath -Force
    Write-Host "Backed up .env.p0-test -> $envBackupName"
}

# Update CHANGELOG.md
$changelog = Join-Path $root 'config\higress\p0-test\CHANGELOG.md'
if (Test-Path -LiteralPath $changelog) {
    $entry = "`n### Backup $version ($((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')))`n" +
             "- Message: $Message`n" +
             "- Backup directory: $backupDir/$version-$safeMessage`n"
    Add-Content -LiteralPath $changelog -Value $entry -Encoding utf8
}

Write-Host ""
Write-Host "Backup complete. Version: $version"
Write-Host "To rollback: powershell -File scripts/Rollback-P0HigressConfig.ps1 -Version '$version-$safeMessage'"
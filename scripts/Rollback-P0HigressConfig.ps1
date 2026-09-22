[CmdletBinding()]
param(
    [switch]$ListVersions,
    [string]$Version,
    [switch]$PreviousVersion
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if ($ListVersions -or (-not $Version -and -not $PreviousVersion)) {
        & python scripts/p0_release.py list
    } else {
        if ($PreviousVersion) {
            $lines = & python scripts/p0_release.py list
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            $previous = @($lines | Where-Object { $_ -match '\brelease\b' } | Select-Object -Skip 1 -First 1)
            if (-not $previous) { throw 'No previous release snapshot is available.' }
            $Version = ($previous -split '\s+')[0]
        }
        if (-not $Version) { throw 'Specify -Version <release-id>, -PreviousVersion, or -ListVersions.' }
        & python scripts/p0_release.py rollback --release-id $Version
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}

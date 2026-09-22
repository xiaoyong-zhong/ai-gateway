[CmdletBinding()]
param([int]$Port = 18770)

$ErrorActionPreference = 'Stop'
if ($Port -lt 1024 -or $Port -gt 65535) {
    throw 'Port must be between 1024 and 65535.'
}
$root = Split-Path -Parent $PSScriptRoot
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw 'Python 3.10+ was not found. Install Python and make sure python is available on PATH.'
}

$hadPreviousPort = Test-Path Env:P0_CONSOLE_PORT
$previousPort = $env:P0_CONSOLE_PORT
$env:P0_CONSOLE_PORT = [string]$Port
$exitCode = 0
Push-Location $root
try {
    & python -X utf8 'gateway/p0-console/server.py'
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
    if ($hadPreviousPort) {
        $env:P0_CONSOLE_PORT = $previousPort
    } else {
        Remove-Item Env:P0_CONSOLE_PORT -ErrorAction SilentlyContinue
    }
}
exit $exitCode

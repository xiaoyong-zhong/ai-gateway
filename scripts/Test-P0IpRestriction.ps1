# P0 IP Restriction 验证脚本
# 测试 IP 白名单是否按预期工作
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts/Test-P0IpRestriction.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/Test-P0IpRestriction.ps1 -UseExplicitLocalHost

[CmdletBinding()]
param(
    [switch]$UseExplicitLocalHost
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env.p0-test'
if (-not (Test-Path -LiteralPath $envFile)) {
    throw 'Missing .env.p0-test. Run scripts/Initialize-P0TestEnvironment.ps1 first.'
}

# Load env
$values = @{}
foreach ($line in Get-Content -LiteralPath $envFile) {
    if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') { $values[$matches[1]] = $matches[2] }
}
$key = $values['P0_HIGRESS_CONSUMER_KEY']
if (-not $key) {
    throw 'P0_HIGRESS_CONSUMER_KEY is missing from .env.p0-test.'
}

$baseUrl = 'http://ai-gateway-test.local:18080'
$headers = @{
    'Authorization' = "Bearer $key"
    'Accept'        = 'application/json'
}
if ($UseExplicitLocalHost) {
    $baseUrl = 'http://127.0.0.1:18080'
    $headers['Host'] = 'ai-gateway-test.local'
}

Write-Host "=== P0 IP Restriction 验证 ==="
Write-Host "Base URL: $baseUrl"
Write-Host ""

function Test-HttpStatus {
    param(
        [string]$Url,
        [hashtable]$Headers,
        [string]$TestName
    )
    try {
        $response = Invoke-WebRequest -Uri $Url -Headers $Headers -Method GET -UseBasicParsing -ErrorAction Stop
        $status = $response.StatusCode
    } catch {
        if ($_.Exception.Response) {
            $status = [int]$_.Exception.Response.StatusCode
        } else {
            Write-Host "FAIL $TestName: Connection failed - $($_.Exception.Message)"
            return $null
        }
    }
    Write-Host "RESULT $TestName: HTTP $status"
    return $status
}

# Test 1: 本地回环地址 (127.0.0.1) 应该在白名单内
$status1 = Test-HttpStatus -Url "$baseUrl/v1/models" -Headers $headers -TestName "Localhost access (expected 200)"
if ($status1 -eq 200) {
    Write-Host "PASS: Localhost (127.0.0.1) is allowed by IP Restriction"
} elseif ($status1 -eq 403) {
    Write-Host "FAIL: Localhost (127.0.0.1) was blocked - verify whitelist includes 127.0.0.1/32"
} else {
    Write-Host "WARN: Unexpected status $status1 for localhost access"
}

# Test 2: 检查 IP Restriction 插件是否生效
# 由于我们在本地 Docker 环境，所有请求都来自 127.0.0.1
# 所以无法直接测试拒绝场景。提供手动测试说明。
Write-Host ""
Write-Host "=== IP Restriction 手动测试说明 ==="
Write-Host "当前 P0 环境绑定 127.0.0.1，所有请求来自回环地址。"
Write-Host "要验证 IP 拒绝行为，请按以下步骤操作："
Write-Host ""
Write-Host "1. 临时从 ip-restriction.json 的 allowlist 中移除 127.0.0.1/32"
Write-Host "2. 重新应用配置:"
Write-Host "   docker compose --env-file .env.p0-test -f deploy/docker-compose.p0-test.yml exec -T higress python3 -X utf8 /p0-scripts/bootstrap_higress_p0.py --env-file /p0-env/.env.p0-test --resource-file /p0-config/resources.json --include-ip-restriction"
Write-Host "3. 执行当前脚本，应看到 403 响应"
Write-Host "4. 恢复 allowlist 中的 127.0.0.1/32 并重新应用配置"
Write-Host ""

# Test 3: 验证拒绝响应格式
Write-Host "=== IP Restriction 配置检查 ==="
$ipConfigFile = Join-Path $root 'config\higress\p0-test\ip-restriction.json'
if (Test-Path -LiteralPath $ipConfigFile) {
    $ipConfig = Get-Content -LiteralPath $ipConfigFile -Raw | ConvertFrom-Json
    $plugin = $ipConfig[0]
    $config = $plugin.spec.matchRules[0].config

    Write-Host "Plugin name: $($plugin.metadata.name)"
    Write-Host "Fail strategy: $($plugin.spec.failStrategy)"
    Write-Host "Whitelist mode: $($config.enable_whitelist_mode)"
    Write-Host "Allowed CIDRs:"
    foreach ($cidr in $config.allowlist) {
        Write-Host "  - $cidr"
    }
    Write-Host "Denied CIDRs: $($config.denylist.Count)"
    Write-Host "Rejected code: $($config.rejected_code)"

    if ($plugin.spec.failStrategy -eq 'FAIL_CLOSE') {
        Write-Host "PASS: IP Restriction 使用 FAIL_CLOSE 策略"
    } else {
        Write-Host "FAIL: IP Restriction 应为 FAIL_CLOSE，当前为 $($plugin.spec.failStrategy)"
    }

    if ($config.enable_whitelist_mode -eq $true) {
        Write-Host "PASS: 白名单模式已启用"
    } else {
        Write-Host "FAIL: 白名单模式应启用"
    }

    if ($config.rejected_code -eq 403) {
        Write-Host "PASS: 拒绝码为 403"
    } else {
        Write-Host "FAIL: 拒绝码应为 403，当前为 $($config.rejected_code)"
    }
} else {
    Write-Host "SKIP: ip-restriction.json not found at $ipConfigFile"
}

Write-Host ""
Write-Host "=== P0 IP Restriction 验证完成 ==="
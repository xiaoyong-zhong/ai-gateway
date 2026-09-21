# P0 端到端验收脚本
# 依次运行所有 P0 验收测试，生成验收报告
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts/Run-P0EndToEnd.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/Run-P0EndToEnd.ps1 -VerifyRateLimit
#   powershell -ExecutionPolicy Bypass -File scripts/Run-P0EndToEnd.ps1 -UseExplicitLocalHost

[CmdletBinding()]
param(
    [switch]$VerifyRateLimit,
    [switch]$UseExplicitLocalHost,
    [string]$OutputDir = "reports"
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env.p0-test'
$composeFile = Join-Path $root 'deploy\docker-compose.p0-test.yml'

if (-not (Test-Path -LiteralPath $envFile)) {
    throw 'Missing .env.p0-test. Run scripts/Initialize-P0TestEnvironment.ps1 first.'
}

# Ensure output directory
$outputFullPath = Join-Path $root $OutputDir
if (-not (Test-Path -LiteralPath $outputFullPath)) {
    New-Item -Path $outputFullPath -ItemType Directory -Force | Out-Null
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$reportFile = Join-Path $outputFullPath "P0-EndToEndReport-$timestamp.md"

$results = @()
$totalTests = 0
$passedTests = 0
$failedTests = 0
$skippedTests = 0

function Add-Result {
    param(
        [string]$Category,
        [string]$TestName,
        [string]$Status,  # PASS, FAIL, SKIP
        [string]$Detail = ""
    )
    $totalTests++
    if ($Status -eq "PASS") { $passedTests++ }
    elseif ($Status -eq "FAIL") { $failedTests++ }
    else { $skippedTests++ }

    $results += [PSCustomObject]@{
        Category = $Category
        TestName = $TestName
        Status = $Status
        Detail = $Detail
    }

    $icon = switch ($Status) {
        "PASS" { "✅" }
        "FAIL" { "❌" }
        "SKIP" { "⏭️" }
    }
    Write-Host "  $icon [$Category] $TestName $($Detail)"
}

Write-Host "=================================================="
Write-Host "  P0 端到端验收测试"
Write-Host "  时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "=================================================="
Write-Host ""

# ============================================================
# Phase 1: 环境检查
# ============================================================
Write-Host "[Phase 1] 环境检查"

# Check containers are running
try {
    $containerCheck = docker compose --env-file $envFile -f $composeFile ps --format json 2>&1
    $runningCount = (docker compose --env-file $envFile -f $composeFile ps 2>&1 | Select-String '\srunning\s' | Measure-Object).Count
    if ($runningCount -ge 4) {
        Add-Result -Category "环境" -TestName "Docker 容器运行中" -Status "PASS" -Detail "($runningCount containers)"
    } else {
        Add-Result -Category "环境" -TestName "Docker 容器运行中" -Status "FAIL" -Detail "Only $runningCount containers running (need >=4)"
    }
} catch {
    Add-Result -Category "环境" -TestName "Docker 容器运行中" -Status "FAIL" -Detail "$($_.Exception.Message)"
}

# Check Higress API
try {
    $certIgnore = [System.Net.ServicePointManager]::ServerCertificateValidationCallback = {$true}
    $response = Invoke-WebRequest -Uri "https://127.0.0.1:18443/version" -UseBasicParsing -TimeoutSeconds 5 -ErrorAction Stop
    Add-Result -Category "环境" -TestName "Higress API 可达" -Status "PASS" -Detail "(127.0.0.1:18443)"
} catch {
    Add-Result -Category "环境" -TestName "Higress API 可达" -Status "FAIL" -Detail "$($_.Exception.Message)"
}

# Check LiteLLM health
try {
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') { $values[$matches[1]] = $matches[2] }
    }
    $masterKey = $values['P0_LITELLM_MASTER_KEY']
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:14000/health" -UseBasicParsing -TimeoutSeconds 5 -ErrorAction Stop
    Add-Result -Category "环境" -TestName "LiteLLM 健康检查" -Status "PASS"
} catch {
    Add-Result -Category "环境" -TestName "LiteLLM 健康检查" -Status "FAIL" -Detail "$($_.Exception.Message)"
}

# Check Redis
try {
    $redisPing = docker compose --env-file $envFile -f $composeFile exec -T redis redis-cli ping 2>&1 | Out-String
    if ($redisPing.Trim() -eq "PONG") {
        Add-Result -Category "环境" -TestName "Redis 连接正常" -Status "PASS"
    } else {
        Add-Result -Category "环境" -TestName "Redis 连接正常" -Status "FAIL" -Detail "Expected PONG, got: $redisPing"
    }
} catch {
    Add-Result -Category "环境" -TestName "Redis 连接正常" -Status "FAIL" -Detail "$($_.Exception.Message)"
}

Write-Host ""

# ============================================================
# Phase 2: 入口和认证 (P0-1, P0-2)
# ============================================================
Write-Host "[Phase 2] 入口和认证验证"

# Load keys
$values = @{}
foreach ($line in Get-Content -LiteralPath $envFile) {
    if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') { $values[$matches[1]] = $matches[2] }
}
$key = $values['P0_HIGRESS_CONSUMER_KEY']
$legacyKey = $values['P0_HIGRESS_LEGACY_CONSUMER_KEY']

$baseUrl = 'http://ai-gateway-test.local:18080'
$hostHeader = $null
if ($UseExplicitLocalHost) {
    $baseUrl = 'http://127.0.0.1:18080'
    $hostHeader = 'ai-gateway-test.local'
}

function Test-HttpCall {
    param(
        [string]$Url,
        [string]$Key = $null,
        [string]$Host = $null,
        [string]$Method = "GET",
        [hashtable]$Payload = $null
    )
    $headers = @{ 'Accept' = 'application/json' }
    if ($Key) { $headers['Authorization'] = "Bearer $Key" }
    if ($Host) { $headers['Host'] = $Host }

    try {
        if ($Payload) {
            $body = $Payload | ConvertTo-Json -Compress
            $response = Invoke-WebRequest -Uri $Url -Headers $headers -Method POST -Body $body -ContentType 'application/json' -UseBasicParsing -ErrorAction Stop
            return @{ Status = $response.StatusCode; Body = $response.Content }
        } else {
            $response = Invoke-WebRequest -Uri $Url -Headers $headers -Method $Method -UseBasicParsing -ErrorAction Stop
            return @{ Status = $response.StatusCode; Body = $response.Content }
        }
    } catch {
        if ($_.Exception.Response) {
            $errReader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
            $errBody = $errReader.ReadToEnd()
            return @{ Status = [int]$_.Exception.Response.StatusCode; Body = $errBody }
        }
        return @{ Status = 0; Body = $_.Exception.Message }
    }
}

# Test: No key
$result = Test-HttpCall -Url "$baseUrl/v1/models"
if ($result.Status -eq 401) {
    Add-Result -Category "认证" -TestName "缺失 Key 返回 401" -Status "PASS" -Detail "(HTTP $($result.Status))"
} else {
    Add-Result -Category "认证" -TestName "缺失 Key 返回 401" -Status "FAIL" -Detail "(Expected 401, got $($result.Status))"
}

# Test: Invalid key
$result = Test-HttpCall -Url "$baseUrl/v1/models" -Key "gw-p0-invalid" -Host $hostHeader
if ($result.Status -eq 401) {
    Add-Result -Category "认证" -TestName "错误 Key 返回 401" -Status "PASS" -Detail "(HTTP $($result.Status))"
} else {
    Add-Result -Category "认证" -TestName "错误 Key 返回 401" -Status "FAIL" -Detail "(Expected 401, got $($result.Status))"
}

# Test: Valid key lists models
$result = Test-HttpCall -Url "$baseUrl/v1/models" -Key $key -Host $hostHeader
if ($result.Status -eq 200) {
    try {
        $data = $result.Body | ConvertFrom-Json
        $modelIds = ($data.data | ForEach-Object { $_.id }) -join ","
        if ($modelIds -match "my-qwen3.6-27b") {
            Add-Result -Category "认证" -TestName "正确 Key 列模型 200" -Status "PASS" -Detail "(models: $($data.data.Count))"
        } else {
            Add-Result -Category "认证" -TestName "正确 Key 列模型 200" -Status "FAIL" -Detail "(my-qwen3.6-27b not in model list)"
        }
    } catch {
        Add-Result -Category "认证" -TestName "正确 Key 列模型 200" -Status "FAIL" -Detail "(Failed to parse JSON)"
    }
} else {
    Add-Result -Category "认证" -TestName "正确 Key 列模型 200" -Status "FAIL" -Detail "(Expected 200, got $($result.Status))"
}

# Test: Legacy key (if exists)
if ($legacyKey -and $legacyKey.Trim() -ne "") {
    $result = Test-HttpCall -Url "$baseUrl/v1/models" -Key $legacyKey -Host $hostHeader
    if ($result.Status -eq 200) {
        Add-Result -Category "认证" -TestName "旧 Key 列模型 200" -Status "PASS" -Detail "(legacy key valid)"
    } else {
        Add-Result -Category "认证" -TestName "旧 Key 列模型 200" -Status "FAIL" -Detail "(Expected 200, got $($result.Status))"
    }
} else {
    Add-Result -Category "认证" -TestName "旧 Key 列模型 200" -Status "SKIP" -Detail "(No legacy key configured)"
}

Write-Host ""

# ============================================================
# Phase 3: 模型调用 (P0-4)
# ============================================================
Write-Host "[Phase 3] 模型调用验证"

# Non-streaming chat
$chatPayload = @{
    model = "my-qwen3.6-27b"
    messages = @(@{ role = "user"; content = "只回复：P0_OK" })
    temperature = 0
    max_tokens = 512
}

try {
    $result = Test-HttpCall -Url "$baseUrl/v1/chat/completions" -Key $key -Host $hostHeader -Payload $chatPayload
    if ($result.Status -eq 200) {
        $data = $result.Body | ConvertFrom-Json
        $content = $data.choices[0].message.content
        $usage = $data.usage
        if ($content -and $content.Trim() -ne "") {
            Add-Result -Category "模型" -TestName "非流式 Chat 返回正文" -Status "PASS" -Detail "(content: $($content.Substring(0, [Math]::Min(50, $content.Length))))"
        } else {
            Add-Result -Category "模型" -TestName "非流式 Chat 返回正文" -Status "FAIL" -Detail "(Empty content)"
        }
        if ($usage -and $usage.total_tokens) {
            Add-Result -Category "模型" -TestName "非流式 Chat 包含 usage" -Status "PASS" -Detail "(total_tokens: $($usage.total_tokens))"
        } else {
            Add-Result -Category "模型" -TestName "非流式 Chat 包含 usage" -Status "FAIL" -Detail "(Missing usage)"
        }
    } else {
        Add-Result -Category "模型" -TestName "非流式 Chat 200" -Status "FAIL" -Detail "(Expected 200, got $($result.Status))"
        Add-Result -Category "模型" -TestName "非流式 Chat 返回正文" -Status "FAIL" -Detail "(Non-200 response)"
        Add-Result -Category "模型" -TestName "非流式 Chat 包含 usage" -Status "FAIL" -Detail "(Non-200 response)"
    }
} catch {
    Add-Result -Category "模型" -TestName "非流式 Chat" -Status "FAIL" -Detail "$($_.Exception.Message)"
    Add-Result -Category "模型" -TestName "非流式 Chat 返回正文" -Status "FAIL" -Detail "(Exception)"
    Add-Result -Category "模型" -TestName "非流式 Chat 包含 usage" -Status "FAIL" -Detail "(Exception)"
}

# Streaming chat
Write-Host "  (streaming test may take 30-60s...)"
try {
    $streamPayload = @{
        model = "my-qwen3.6-27b"
        messages = @(@{ role = "user"; content = "只回复：P0_STREAM_OK" })
        temperature = 0
        max_tokens = 256
        stream = $true
        stream_options = @{ include_usage = $true }
    } | ConvertTo-Json -Compress

    $headers = @{
        'Authorization' = "Bearer $key"
        'Content-Type'  = 'application/json'
        'Accept'        = 'text/event-stream'
    }
    if ($hostHeader) { $headers['Host'] = $hostHeader }

    $certIgnore = [System.Net.ServicePointManager]::ServerCertificateValidationCallback = {$true}

    $request = [System.Net.HttpWebRequest]::Create("$baseUrl/v1/chat/completions")
    $request.Method = 'POST'
    $request.Timeout = 120000
    foreach ($h in $headers.Keys) { $request.Headers[$h] = $headers[$h] }
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($streamPayload)
    $request.ContentLength = $bodyBytes.Length
    $reqStream = $request.GetRequestStream()
    $reqStream.Write($bodyBytes, 0, $bodyBytes.Length)
    $reqStream.Close()

    $response = $request.GetResponse()
    $sawText = $false
    $sawDone = $false
    $sawUsage = $false

    if ($response.StatusCode -eq 200 -and $response.ContentType -match 'event-stream') {
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        while (-not $reader.EndOfStream) {
            $line = $reader.ReadLine()
            if ($line -match '^data:\s*(.*)') {
                $event = $matches[1].Trim()
                if ($event -eq '[DONE]') { $sawDone = $true; break }
                if ($event -ne '') {
                    try {
                        $chunk = $event | ConvertFrom-Json
                        $content = $chunk.choices[0].delta.content
                        if ($content -and $content.Trim()) { $sawText = $true }
                        if ($chunk.usage) { $sawUsage = $true }
                    } catch {}
                }
            }
        }
        $reader.Close()
        $response.Close()

        Add-Result -Category "模型" -TestName "SSE 流式 Chat 200" -Status "PASS"
        if ($sawText) {
            Add-Result -Category "模型" -TestName "SSE 流式返回文本" -Status "PASS"
        } else {
            Add-Result -Category "模型" -TestName "SSE 流式返回文本" -Status "FAIL" -Detail "(No text in stream)"
        }
        if ($sawDone) {
            Add-Result -Category "模型" -TestName "SSE 流式以 [DONE] 结束" -Status "PASS"
        } else {
            Add-Result -Category "模型" -TestName "SSE 流式以 [DONE] 结束" -Status "FAIL" -Detail "(No [DONE] event)"
        }
        if ($sawUsage) {
            Add-Result -Category "模型" -TestName "SSE 流式包含 usage" -Status "PASS"
        } else {
            Add-Result -Category "模型" -TestName "SSE 流式包含 usage" -Status "FAIL" -Detail "(No usage in stream)"
        }
    } else {
        Add-Result -Category "模型" -TestName "SSE 流式 Chat 200" -Status "FAIL" -Detail "(Expected 200 + event-stream, got $($response.StatusCode))"
    }
} catch {
    Add-Result -Category "模型" -TestName "SSE 流式 Chat" -Status "FAIL" -Detail "$($_.Exception.Message)"
}

Write-Host ""

# ============================================================
# Phase 4: 限流 (P0-6)
# ============================================================
Write-Host "[Phase 4] 限流验证"

if ($VerifyRateLimit) {
    Write-Host "  Exhausting RPM limit (this will make rapid requests)..."
    $limited = $false
    for ($i = 1; $i -le 15; $i++) {
        $result = Test-HttpCall -Url "$baseUrl/v1/models" -Key $key -Host $hostHeader
        if ($result.Status -eq 429) {
            $limited = $true
            Add-Result -Category "限流" -TestName "10 RPM 限流返回 429" -Status "PASS" -Detail "(triggered at request #$i)"
            break
        }
        Start-Sleep -Milliseconds 100
    }
    if (-not $limited) {
        Add-Result -Category "限流" -TestName "10 RPM 限流返回 429" -Status "FAIL" -Detail "(No 429 after 15 requests)"
    }
} else {
    Add-Result -Category "限流" -TestName "10 RPM 限流返回 429" -Status "SKIP" -Detail "(Add -VerifyRateLimit to test)"
}

Write-Host ""

# ============================================================
# Phase 5: 可观测性 (P0-5)
# ============================================================
Write-Host "[Phase 5] 可观测性验证"

# Run observability check
try {
    $obsOutput = & powershell -ExecutionPolicy Bypass -File (Join-Path $root 'scripts\Verify-P0Observability.ps1') 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0 -and $obsOutput -match 'PASS') {
        Add-Result -Category "可观测性" -TestName "SpendLog 记录完整性" -Status "PASS" -Detail "($obsOutput)"
    } else {
        Add-Result -Category "可观测性" -TestName "SpendLog 记录完整性" -Status "FAIL" -Detail "($obsOutput)"
    }
} catch {
    Add-Result -Category "可观测性" -TestName "SpendLog 记录完整性" -Status "FAIL" -Detail "$($_.Exception.Message)"
}

# Check for secret leak (the Verify-P0Observability script checks this)
if ($obsOutput -match 'secret_leak_rows=0') {
    Add-Result -Category "可观测性" -TestName "密钥脱敏验证" -Status "PASS" -Detail "(secret_leak_rows=0)"
} elseif ($obsOutput -match 'secret_leak_rows=(\d+)') {
    $leakCount = $Matches[1]
    Add-Result -Category "可观测性" -TestName "密钥脱敏验证" -Status "FAIL" -Detail "(secret_leak_rows=$leakCount)"
} else {
    Add-Result -Category "可观测性" -TestName "密钥脱敏验证" -Status "SKIP" -Detail "(Could not parse leak count)"
}

# AI Statistics
try {
    $statsOutput = & powershell -ExecutionPolicy Bypass -File (Join-Path $root 'scripts\Verify-P0AiStatistics.ps1') 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) {
        Add-Result -Category "可观测性" -TestName "AI Statistics 指标采集" -Status "PASS"
    } else {
        Add-Result -Category "可观测性" -TestName "AI Statistics 指标采集" -Status "FAIL" -Detail "($statsOutput)"
    }
} catch {
    Add-Result -Category "可观测性" -TestName "AI Statistics 指标采集" -Status "FAIL" -Detail "$($_.Exception.Message)"
}

Write-Host ""

# ============================================================
# Phase 6: IP Restriction (P0-6)
# ============================================================
Write-Host "[Phase 6] IP Restriction 验证"

$ipConfigFile = Join-Path $root 'config\higress\p0-test\ip-restriction.json'
if (Test-Path -LiteralPath $ipConfigFile) {
    $ipConfig = Get-Content -LiteralPath $ipConfigFile -Raw | ConvertFrom-Json
    $plugin = $ipConfig[0]
    $config = $plugin.spec.matchRules[0].config

    if ($plugin.spec.failStrategy -eq 'FAIL_CLOSE') {
        Add-Result -Category "IP控制" -TestName "IP Restriction FAIL_CLOSE" -Status "PASS"
    } else {
        Add-Result -Category "IP控制" -TestName "IP Restriction FAIL_CLOSE" -Status "FAIL" -Detail "(Got $($plugin.spec.failStrategy))"
    }

    if ($config.enable_whitelist_mode) {
        Add-Result -Category "IP控制" -TestName "白名单模式启用" -Status "PASS" -Detail "($($config.allowlist.Count) CIDRs)"
    } else {
        Add-Result -Category "IP控制" -TestName "白名单模式启用" -Status "FAIL"
    }

    # Check localhost is in whitelist
    $hasLocalhost = $config.allowlist -contains '127.0.0.1/32'
    if ($hasLocalhost) {
        Add-Result -Category "IP控制" -TestName "127.0.0.1 在白名单内" -Status "PASS"
    } else {
        Add-Result -Category "IP控制" -TestName "127.0.0.1 在白名单内" -Status "FAIL"
    }

    # Check P0 network is in whitelist
    $hasP0Net = $config.allowlist -contains '172.30.52.0/24'
    if ($hasP0Net) {
        Add-Result -Category "IP控制" -TestName "P0 网络在白名单内" -Status "PASS"
    } else {
        Add-Result -Category "IP控制" -TestName "P0 网络在白名单内" -Status "FAIL"
    }
} else {
    Add-Result -Category "IP控制" -TestName "IP Restriction 配置存在" -Status "FAIL" -Detail "(ip-restriction.json not found)"
}

Write-Host ""

# ============================================================
# Phase 7: 配置版本/回滚 (P0-7)
# ============================================================
Write-Host "[Phase 7] 配置版本/回滚验证"

# Check backup script exists
$backupScript = Join-Path $root 'scripts\Backup-P0Config.ps1'
if (Test-Path -LiteralPath $backupScript) {
    Add-Result -Category "版本管理" -TestName "备份脚本存在" -Status "PASS"
} else {
    Add-Result -Category "版本管理" -TestName "备份脚本存在" -Status "FAIL"
}

$rollbackScript = Join-Path $root 'scripts\Rollback-P0HigressConfig.ps1'
if (Test-Path -LiteralPath $rollbackScript) {
    Add-Result -Category "版本管理" -TestName "回滚脚本存在" -Status "PASS"
} else {
    Add-Result -Category "版本管理" -TestName "回滚脚本存在" -Status "FAIL"
}

$deployReportScript = Join-Path $root 'scripts\Generate-P0DeployReport.ps1'
if (Test-Path -LiteralPath $deployReportScript) {
    Add-Result -Category "版本管理" -TestName "部署报告脚本存在" -Status "PASS"
} else {
    Add-Result -Category "版本管理" -TestName "部署报告脚本存在" -Status "FAIL"
}

$changelogFile = Join-Path $root 'config\higress\p0-test\CHANGELOG.md'
if (Test-Path -LiteralPath $changelogFile) {
    Add-Result -Category "版本管理" -TestName "CHANGELOG.md 存在" -Status "PASS"
} else {
    Add-Result -Category "版本管理" -TestName "CHANGELOG.md 存在" -Status "FAIL"
}

Write-Host ""

# ============================================================
# Generate Report
# ============================================================
Write-Host "=================================================="
Write-Host "  验收报告生成"
Write-Host "=================================================="

$report = @"
# P0 端到端验收报告

**时间**: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
**Git Branch**: $(git -C $root rev-parse --abbrev-ref HEAD 2>$null)
**Git Commit**: $(git -C $root rev-parse --short HEAD 2>$null)

## 总览

| 指标 | 数量 |
|---|---|
| 总测试数 | $totalTests |
| 通过 | $passedTests |
| 失败 | $failedTests |
| 跳过 | $skippedTests |
| 通过率 | $($passedTests / [Math]::Max(1, ($totalTests - $skippedTests)) * 100).ToString('F1')% |

## 详细结果

| 分类 | 测试项 | 状态 | 备注 |
|---|---|---|---|
"@

foreach ($r in $results) {
    $icon = switch ($r.Status) {
        "PASS" { "✅" }
        "FAIL" { "❌" }
        "SKIP" { "⏭️" }
    }
    $report += "`n| $($r.Category) | $($r.TestName) | $icon $($r.Status) | $($r.Detail) |"
}

$report += @"

## 结论

"@

if ($failedTests -eq 0) {
    $report += "**全部测试通过** ✅ - P0 验收通过`n"
} else {
    $report += "**存在 $failedTests 项失败** ❌ - 需修复后重新验收`n"
}

$report += @"

## 附录

### 环境信息
- 测试域名: `ai-gateway-test.local:18080`
- Higress 管理端口: `127.0.0.1:18001`
- LiteLLM 端口: `127.0.0.1:14000`
- Docker 网络: `172.30.52.0/24`

### 使用的脚本
- `scripts/Initialize-P0TestEnvironment.ps1` - 环境初始化
- `scripts/Test-P0Gateway.ps1` - 网关验收
- `scripts/Verify-P0Observability.ps1` - 可观测性验证
- `scripts/Verify-P0AiStatistics.ps1` - AI 指标验证
- `scripts/Test-P0IpRestriction.ps1` - IP 限制验证
- `scripts/Backup-P0Config.ps1` - 配置备份
- `scripts/Rollback-P0HigressConfig.ps1` - 配置回滚
- `scripts/Generate-P0DeployReport.ps1` - 部署报告
- `scripts/Run-P0EndToEnd.ps1` - 端到端验收 (本脚本)

---
*报告由 scripts/Run-P0EndToEnd.ps1 自动生成*
"@

$report | Set-Content -LiteralPath $reportFile -Encoding utf8

Write-Host ""
Write-Host "=== 验收总览 ==="
Write-Host "总测试: $totalTests  |  通过: $passedTests  |  失败: $failedTests  |  跳过: $skippedTests"
Write-Host ""
Write-Host "报告已保存到: $reportFile"

if ($failedTests -gt 0) {
    exit 1
}
exit 0
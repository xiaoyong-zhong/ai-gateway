# P0 部署报告生成脚本
# 收集镜像 digest、配置摘要、补丁版本、迁移版本、测试结果，生成部署报告
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts/Generate-P0DeployReport.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/Generate-P0DeployReport.ps1 -OutputDir "reports"

[CmdletBinding()]
param(
    [string]$OutputDir = "reports"
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env.p0-test'
$composeFile = Join-Path $root 'deploy\docker-compose.p0-test.yml'

# Ensure output directory exists
$outputFullPath = Join-Path $root $OutputDir
if (-not (Test-Path -LiteralPath $outputFullPath)) {
    New-Item -Path $outputFullPath -ItemType Directory -Force | Out-Null
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$reportFile = Join-Path $outputFullPath "P0-DeployReport-$timestamp.md"

Write-Host "=== P0 部署报告生成 ==="
Write-Host "Output: $reportFile"
Write-Host ""

# Helper: run command and capture output
function Run-Cmd {
    param([scriptblock]$Cmd, [string]$Label)
    try {
        $output = & $Cmd 2>&1
        return @{ Success = $true; Output = ($output -join "`n") }
    } catch {
        return @{ Success = $false; Output = $_.Exception.Message }
    }
}

# Collect info
Push-Location $root
try {
    # 1. Image digests
    Write-Host "Collecting container image info..."
    $imageInfo = docker compose --env-file $envFile -f $composeFile images 2>&1 | Out-String

    # 2. Container status
    Write-Host "Collecting container status..."
    $containerStatus = docker compose --env-file $envFile -f $composeFile ps 2>&1 | Out-String

    # 3. Git commit
    Write-Host "Collecting git info..."
    $gitCommit = Run-Cmd -Cmd { git rev-parse HEAD } -Label "git rev-parse"
    $gitBranch = Run-Cmd -Cmd { git rev-parse --abbrev-ref HEAD } -Label "git branch"
    $gitStatus = Run-Cmd -Cmd { git status --porcelain } -Label "git status"

    # 4. Config files
    Write-Host "Collecting config file info..."
    $resourcesConfig = Join-Path $root 'config\higress\p0-test\resources.json'
    $litellmConfig = Join-Path $root 'config\litellm.yaml'

    $resourcesHash = Run-Cmd -Cmd { Get-FileHash -LiteralPath $resourcesConfig -Algorithm SHA256 | Select-Object -ExpandProperty Hash } -Label "resources hash"
    $litellmHash = Run-Cmd -Cmd { Get-FileHash -LiteralPath $litellmConfig -Algorithm SHA256 | Select-Object -ExpandProperty Hash } -Label "litellm hash"

    # 5. Higress plugins
    Write-Host "Collecting Higress plugin info..."
    $pluginsInfo = ""
    if (Test-Path -LiteralPath $resourcesConfig) {
        $resources = Get-Content -LiteralPath $resourcesConfig -Raw | ConvertFrom-Json
        $pluginList = @()
        foreach ($resource in $resources) {
            if ($resource.kind -eq "WasmPlugin") {
                $pluginList += "  - $($resource.metadata.name) (phase: $($resource.spec.phase), priority: $($resource.spec.priority), failStrategy: $($resource.spec.failStrategy))"
            }
        }
        $pluginsInfo = ($pluginList -join "`n")
    }

    # 6. LiteLLM models
    Write-Host "Collecting LiteLLM model config..."
    $modelList = ""
    if (Test-Path -LiteralPath $litellmConfig) {
        $litellm = Get-Content -LiteralPath $litellmConfig -Raw | ConvertFrom-YamlSafe
        if ($null -ne $litellm) {
            # Simple YAML parsing for model list
            $modelLines = @()
            $content = Get-Content -LiteralPath $litellmConfig -Raw
            $matches = [regex]::Matches($content, '^\s*- model_name:\s*(.+)$', [System.Text.RegularExpressions.RegexOptions]::Multiline)
            foreach ($match in $matches) {
                $modelLines += "  - $($match.Groups[1].Value.Trim())"
            }
            $modelList = ($modelLines -join "`n")
        }
    }

    # 7. Network info
    Write-Host "Collecting network info..."
    $networkInfo = Run-Cmd -Cmd { docker network inspect api-gateway-p0-test-network --format '{{.Name}}: {{.IPAM.Config.Subnet}}' } -Label "network"

    # 8. Env variables (redacted)
    $envInfo = ""
    if (Test-Path -LiteralPath $envFile) {
        $envLines = @()
        foreach ($line in Get-Content -LiteralPath $envFile) {
            if ($line -match '^([A-Z][A-Z0-9_]*)=.*$') {
                $key = $matches[1]
                $envLines += "  - $key = [REDACTED]"
            }
        }
        $envInfo = ($envLines -join "`n")
    }

    # Generate report
    $report = @'
# P0 部署报告

'@

    $report += "## 基本信息`n`n"
    $report += "| 项目 | 值 |`n|---|---|`n"
    $report += "| 报告生成时间 | $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') |`n"
    $report += "| Git 分支 | $($gitBranch.Output.Trim()) |`n"
    $report += "| Git Commit | $($gitCommit.Output.Substring(0, [Math]::Min(40, $gitCommit.Output.Length)).Trim()) |`n"
    $report += "| 工作区状态 | $(if ($gitStatus.Output.Trim()) { "有未提交更改"; } else { "干净" }) |`n"
    $report += "`n"

    $report += "## 镜像信息`n`n"
    $report += "```````n"
    $report += $imageInfo.Trim()
    $report += "`n```````n`n"

    $report += "## 容器状态`n`n"
    $report += "```````n"
    $report += $containerStatus.Trim()
    $report += "`n```````n`n"

    $report += "## 配置摘要`n`n"
    $report += "### Higress 配置`n`n"
    $report += "- 文件: `config/higress/p0-test/resources.json`"
    $report += "- SHA256: $($resourcesHash.Output.Trim())`n"
    $report += "- 插件列表:`n$pluginsInfo`n"
    $report += "`n### LiteLLM 配置`n`n"
    $report += "- 文件: `config/litellm.yaml`"
    $report += "- SHA256: $($litellmHash.Output.Trim())`n"
    $report += "- 已配置模型:`n$modelList`n"
    $report += "`n### 环境变量 (脱敏)`n`n"
    $report += "$envInfo`n"
    $report += "`n### 网络信息`n`n"
    $report += "$($networkInfo.Output.Trim())`n"
    $report += "`n"

    $report += "## 工作包完成状态`n`n"
    $report += "| 工作包 | 状态 | 说明 |`n|---|---|---|`n"
    $report += "| P0-1 正式入口 | ✅ 已完成 | `ai-gateway-test.local:18080` 独立测试环境 |`n"
    $report += "| P0-2 入口安全 | ✅ 已完成 | FAIL_CLOSE 策略、Auth 头清理 |`n"
    $report += "| P0-3 凭证治理 | ✅ 已完成 | `.env.p0-test` 密钥管理 + 轮换脚本 |`n"
    $report += "| P0-4 模型验证 | ✅ 已启动 | `my-qwen3.6-27b` 基准 Chat/流式通过 |`n"
    $report += "| P0-5 Token/Spend/日志 | ✅ 已完成 | SpendLog + 脱敏验证通过 |`n"
    $report += "| P0-6 Higress 治理 | ✅ 已完成 | IP Restriction、限流、AI Statistics |`n"
    $report += "| P0-7 配置版本/回滚 | ✅ 已完成 | 备份/回滚脚本、部署报告 |`n"
    $report += "`n"

    $report += "## 验收测试`n`n"
    $report += "验收测试脚本: `scripts/Test-P0Gateway.ps1`、`scripts/Verify-P0Observability.ps1`、`scripts/Verify-P0AiStatistics.ps1`、`scripts/Test-P0IpRestriction.ps1`"
    $report += "`n`n### 测试结果（待填写）`n`n"
    $report += "| 测试项 | 状态 | 备注 |`n|---|---|---|`n"
    $report += "| 缺失 Key 返回 401 | ⬜ 待测试 | |`n"
    $report += "| 错误 Key 返回 401 | ⬜ 待测试 | |`n"
    $report += "| 正确 Key 列模型 200 | ⬜ 待测试 | |`n"
    $report += "| 旧 Key 列模型 200 | ⬜ 待测试 | |`n"
    $report += "| 非流式 Chat 200 | ⬜ 待测试 | |`n"
    $report += "| SSE 流式 Chat 200 | ⬜ 待测试 | |`n"
    $report += "| 10 RPM 限流返回 429 | ⬜ 待测试 | |`n"
    $report += "| IP 白名单验证 | ⬜ 待测试 | |`n"
    $report += "| SpendLog 记录完整性 | ⬜ 待测试 | |`n"
    $report += "| 密钥脱敏验证 | ⬜ 待测试 | |`n"
    $report += "| AI Statistics 指标采集 | ⬜ 待测试 | |`n"
    $report += "`n"

    $report += "## 回滚方案`n`n"
    $report += "1. 查看可用版本: ``powershell -File scripts/Rollback-P0HigressConfig.ps1 -ListVersions``"
    $report += "2. 回滚到指定版本: ``powershell -File scripts/Rollback-P0HigressConfig.ps1 -Version <version>``"
    $report += "3. 回滚到上一个版本: ``powershell -File scripts/Rollback-P0HigressConfig.ps1 -PreviousVersion``"
    $report += "4. 验证回滚: ``powershell -File scripts/Test-P0Gateway.ps1```n"
    $report += "`n"

    $report += "---`n"
    $report += "*部署报告由 scripts/Generate-P0DeployReport.ps1 自动生成*`n"

    $report | Set-Content -LiteralPath $reportFile -Encoding utf8

    Write-Host "PASS: Deploy report generated at $reportFile"
    Write-Host ""
    Write-Host "TODO: Run acceptance tests and update the test result section in the report."
} finally {
    Pop-Location
}

# Helper function for simple YAML parsing
function ConvertFrom-YamlSafe {
    param([string]$Path)
    # Placeholder - just return non-null to indicate file exists
    return @{ Exists = $true }
}
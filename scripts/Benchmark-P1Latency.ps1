[CmdletBinding()]
param(
    [int]$Runs = 3
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root 'deploy/docker-compose.p1-test.yml'
$envFile = Join-Path $root '.env.p1-test'
$higressEnvFile = Join-Path $root '.env.p1-higress-test'
$appsFile = Join-Path $root 'config/p1-test/apps.json'

function Get-EnvMap([string]$Path) {
    $map = @{}
    Get-Content -LiteralPath $Path -Encoding ascii | ForEach-Object {
        if ($_ -match '^([^#=]+)=(.*)$') { $map[$matches[1]] = $matches[2] }
    }
    return $map
}

function Invoke-Composed {
    param(
        [string]$Service,
        [string]$PythonCode
    )
    $tmpFile = [System.IO.Path]::GetTempFileName() + '.py'
    try {
        Set-Content -LiteralPath $tmpFile -Value $PythonCode -Encoding utf8 -NoNewline
        docker cp $tmpFile "${Service}:/tmp/_bench.py" | Out-Null
        docker exec -e RUNS=$Runs "${Service}" python /tmp/_bench.py
    } finally {
        docker exec "${Service}" rm -f /tmp/_bench.py 2>$null
        Remove-Item -LiteralPath $tmpFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-Stats([System.Collections.Generic.List[double]]$Times) {
    if ($times.Count -eq 0) { return $null }
    [pscustomobject]@{
        Count = $times.Count
        Min   = ($times | Measure-Object -Minimum).Minimum
        Med   = ($times | Sort-Object | Select-Object -Skip ([math]::Floor($times.Count / 2)) -First 1)
        Max   = ($times | Measure-Object -Maximum).Maximum
        Avg   = ($times | Measure-Object -Average).Average
    }
}

function Parse-BenchOutput {
    param([string[]]$Output, [System.Collections.Generic.List[double]]$Times)
    foreach ($line in $output) {
        if ($line -match '^(\d{3})\s+(\d+)') {
            $code = [int]$matches[1]
            $ms = [double]$matches[2]
            $Times.Add($ms)
            $color = if ($code -eq 200) { 'Green' } else { 'Red' }
            Write-Host "  HTTP $code  : $($ms.ToString('F0')) ms" -ForegroundColor $color
        }
    }
}

if (-not (Test-Path $envFile) -or -not (Test-Path $higressEnvFile)) {
    throw 'Missing P1 environment files. Run Initialize-P1TestEnvironment.ps1 first.'
}

$keys = Get-EnvMap $higressEnvFile
$app = (Get-Content -LiteralPath $appsFile -Raw -Encoding utf8 | ConvertFrom-Json).applications[0]
$appAKey = $keys[$app.higress_key_env]
$envVars = Get-EnvMap $envFile
$litellmMasterKey = $envVars['P1_LITELLM_MASTER_KEY']

if ([string]::IsNullOrWhiteSpace($appAKey)) { throw "$($app.higress_key_env) not found in .env.p1-higress-test" }
if ([string]::IsNullOrWhiteSpace($litellmMasterKey)) { throw 'P1_LITELLM_MASTER_KEY not found in .env.p1-test' }

$model = $app.models[0]
$appId = $app.app_id

$bodyJson = @{
    model = $model
    messages = @(@{ role = 'user'; content = 'Hi' })
    max_tokens = 1
} | ConvertTo-Json -Compress

# Get container names
$svcName = 'api-gateway-p1-test'
$mapperContainer = "${svcName}-mapper-1"
$litellmContainer = "${svcName}-litellm-1"

Write-Host ''
Write-Host '========================================' -ForegroundColor Cyan
Write-Host '  P1 latency benchmark' -ForegroundColor Cyan
Write-Host "  每层发送 $Runs 轮请求 (ms)" -ForegroundColor DarkGray
Write-Host '========================================' -ForegroundColor Cyan
Write-Host ''

# ---------------------------------------------------------------
# Layer 1: Mapper 容器内部自测（ext-auth 侧车耗时）
# ---------------------------------------------------------------
Write-Host '[Layer 1] Mapper 容器自测  (ext-auth 侧车耗时)' -ForegroundColor Yellow
$mapperTimes = [System.Collections.Generic.List[double]]::new()
$mapperScript = @"
import urllib.request, os, time
app_id = """$appId"""
runs = int(os.environ.get("RUNS", "3"))
for i in range(1, runs + 1):
    start = time.monotonic()
    req = urllib.request.Request(
        "http://127.0.0.1:8081/authorize",
        method="POST",
        headers={
            "x-p1-mapper-request": "p1-ext-auth",
            "x-mse-consumer": app_id,
        },
    )
    r = urllib.request.urlopen(req, timeout=5)
    elapsed = (time.monotonic() - start) * 1000
    print(f"{r.status} {elapsed:.0f}")
"@
try {
    $output = Invoke-Composed -Service $mapperContainer -PythonCode $mapperScript
    Parse-BenchOutput -Output $output -Times $mapperTimes
} catch {
    Write-Host "  [ERROR] $_" -ForegroundColor Red
}
$ms = Get-Stats $mapperTimes
if ($ms) { Write-Host "  -> Min=$($ms.Min.ToString('F0'))  Med=$($ms.Med.ToString('F0'))  Max=$($ms.Max.ToString('F0'))  Avg=$($ms.Avg.ToString('F0')) ms" -ForegroundColor White }
Write-Host ''

# ---------------------------------------------------------------
# Layer 2: LiteLLM 容器直调（绕过 Higress，含上游 Qwen + DB）
# ---------------------------------------------------------------
Write-Host '[Layer 2] LiteLLM 直调  (绕过 Higress, 含上游 Qwen + DB)' -ForegroundColor Yellow
$litellmTimes = [System.Collections.Generic.List[double]]::new()
$litellmScript = @"
import urllib.request, json, os, time
model = os.environ.get("MODEL", "$model")
key = os.environ.get("KEY", "")
runs = int(os.environ.get("RUNS", "3"))
body = json.dumps({
    "model": model,
    "messages": [{"role": "user", "content": "Hi"}],
    "max_tokens": 1,
}).encode()
for i in range(1, runs + 1):
    start = time.monotonic()
    req = urllib.request.Request(
        "http://127.0.0.1:4000/v1/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type":  "application/json",
        },
    )
    r = urllib.request.urlopen(req, timeout=120)
    elapsed = (time.monotonic() - start) * 1000
    print(f"{r.status} {elapsed:.0f}")
"@
try {
    # Need to pass KEY as env var for security
    $tmpFile = [System.IO.Path]::GetTempFileName() + '.py'
    Set-Content -LiteralPath $tmpFile -Value $litellmScript -Encoding utf8 -NoNewline
    docker cp $tmpFile "${litellmContainer}:/tmp/_bench.py" | Out-Null
    $output = docker exec -e RUNS=$Runs -e KEY=$litellmMasterKey "${litellmContainer}" python /tmp/_bench.py
    docker exec "${litellmContainer}" rm -f /tmp/_bench.py 2>$null
    Remove-Item -LiteralPath $tmpFile -Force
    Parse-BenchOutput -Output $output -Times $litellmTimes
} catch {
    Write-Host "  [ERROR] $_" -ForegroundColor Red
}
$ls = Get-Stats $litellmTimes
if ($ls) { Write-Host "  -> Min=$($ls.Min.ToString('F0'))  Med=$($ls.Med.ToString('F0'))  Max=$($ls.Max.ToString('F0'))  Avg=$($ls.Avg.ToString('F0')) ms" -ForegroundColor White }
Write-Host ''

# ---------------------------------------------------------------
# Layer 3: Higress 全链路（从宿主机发起，完整链路）
# ---------------------------------------------------------------
Write-Host '[Layer 3] Higress 全链路  (key-auth + ext-auth + ai-proxy + LiteLLM + Qwen)' -ForegroundColor Yellow
$fullTimes = [System.Collections.Generic.List[double]]::new()
for ($i = 1; $i -le $Runs; $i++) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 `
            -Uri 'http://127.0.0.1:28080/v1/chat/completions' `
            -Method Post `
            -Headers @{ Authorization = 'Bearer ' + $appAKey; Host = 'ai-gateway-p1-test.local' } `
            -ContentType 'application/json' `
            -Body $bodyJson
        $sw.Stop()
        $fullTimes.Add($sw.ElapsedMilliseconds)
        Write-Host "  HTTP $($resp.StatusCode)  : $($sw.ElapsedMilliseconds.ToString('F0')) ms" -ForegroundColor Green
    } catch {
        $sw.Stop()
        $msg = $_.Exception.Message
        if ($msg.Length -gt 80) { $msg = $msg.Substring(0, 80) + '...' }
        Write-Host "  ERROR  : $($sw.ElapsedMilliseconds.ToString('F0')) ms  ($msg)" -ForegroundColor Red
    }
}
$fs = Get-Stats $fullTimes
if ($fs) { Write-Host "  -> Min=$($fs.Min.ToString('F0'))  Med=$($fs.Med.ToString('F0'))  Max=$($fs.Max.ToString('F0'))  Avg=$($fs.Avg.ToString('F0')) ms" -ForegroundColor White }
Write-Host ''

# ---------------------------------------------------------------
# 分析
# ---------------------------------------------------------------
Write-Host '========================================' -ForegroundColor Cyan
Write-Host '  延迟拆解分析' -ForegroundColor Cyan
Write-Host '========================================' -ForegroundColor Cyan

if (($fullTimes.Count -gt 0) -and ($litellmTimes.Count -gt 0)) {
    $fullAvg = ($fullTimes | Measure-Object -Average).Average
    $llAvg = ($litellmTimes | Measure-Object -Average).Average
    $higressOverhead = $fullAvg - $llAvg

    Write-Host ''
    Write-Host "  Layer 3 (全链路) 平均    : $([math]::Round($fullAvg, 0)) ms"
    Write-Host "  Layer 2 (LiteLLM) 平均   : $([math]::Round($llAvg, 0)) ms"
    Write-Host "  Higress 额外开销         : $([math]::Round($higressOverhead, 0)) ms ($([math]::Round($higressOverhead / $fullAvg * 100, 0))%)"
    Write-Host ''

    if ($mapperTimes.Count -gt 0) {
        $mapperAvg = ($mapperTimes | Measure-Object -Average).Average
        Write-Host "  Layer 1 (Mapper) 平均    : $([math]::Round($mapperAvg, 0)) ms"
        Write-Host ''
    }

    if ($higressOverhead -gt 500) {
        Write-Host '  [!] Higress 插件链开销 > 500ms，是主要延迟来源' -ForegroundColor Red
        Write-Host '      ext-auth 同步调用 Mapper + WASM 插件串行是最大嫌疑' -ForegroundColor Yellow
    } elseif ($llAvg -gt $fullAvg * 0.7) {
        Write-Host '  [!] LiteLLM + 上游 Qwen 占用大部分延迟' -ForegroundColor Yellow
        Write-Host '      延迟主要来源于上游 Qwen API (test2-aigc.campusapp.com.cn)' -ForegroundColor Yellow
    } else {
        Write-Host '  [OK] 各层延迟分配合理' -ForegroundColor Green
    }
}

Write-Host ''
Write-Host '提示：Layer 1 & 2 是容器内本地调用，不含跨容器网络开销' -ForegroundColor DarkGray
Write-Host '       Layer 3 包含完整的跨容器调用 (Higress->Mapper, Higress->LiteLLM)' -ForegroundColor DarkGray

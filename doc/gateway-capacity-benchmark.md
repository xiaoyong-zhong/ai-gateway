# 网关容量压测

## 测试范围

使用独立 Docker 项目 `api-gateway-benchmark`，只连接本地模拟上游。
模型名固定为 `benchmark-chat`，密钥固定为测试用的 `sk-benchmark`；不会访问真实模型或消耗供应商额度。
现有 `8080/4000` 服务及真实模型配置不变。测试端口仅绑定本机回环地址。

| TARGET | 请求路径 | 目的 |
| --- | --- | --- |
| mock | k6 -> 模拟上游 | 验证负载发生器和模拟上游有足够余量 |
| litellm | k6 -> LiteLLM -> 模拟上游 | 测模型代理层 |
| higress | k6 -> Higress -> 模拟上游 | 测 Higress 转发层 |
| full | k6 -> Higress -> LiteLLM -> 模拟上游 | 测完整网关组合 |

采用 k6 的连接复用；当前 Python resilience 脚本用于功能和小规模并发，不适合判定高吞吐网关容量。
LiteLLM 测试配置显式关闭重试、无 fallback，未配置响应缓存。
Higress 测试路由仅转发，不启用额外鉴权、限流或审计插件；LiteLLM 和模拟上游校验测试密钥。
这是基础转发配置的测量，部署业务插件、日志和数据库后应按真实配置重测。

## 启动

在项目根目录 PowerShell 执行：

```powershell
docker compose -f deploy/docker-compose.benchmark.yml up -d --wait --wait-timeout 180
```

需要 Docker Desktop。第一次会拉取镜像并启动服务；k6 在第一次运行时拉取镜像。
使用子网 `172.30.51.0/24`，不要与已有网络冲突。

| 服务 | 本机地址 |
| --- | --- |
| 模拟上游 | http://localhost:19000/v1 |
| LiteLLM | http://localhost:14000/v1 |
| Higress 完整链路 | http://localhost:18080/v1 |
| Higress 控制台 | http://localhost:18001 |

单独 Higress 转发路径也使用 18080，但要带 `Host: mock.benchmark.local`；k6 的 `TARGET=higress` 自动处理。
测试环境只有模拟模型。现有真实模型压测脚本默认仍访问原来的 8080。

## 预热

每次重启、调整模型代理或切换测试条件后先预热，预热报告不要用于容量结论。
不同目标逐一执行，不要同时运行四组压测，否则会互相争抢资源。

```powershell
foreach ($target in @('mock','litellm','higress','full')) {
    docker compose -f deploy/docker-compose.benchmark.yml run --rm -e "TARGET=$target" -e LOAD_MODE=rate -e RPS=5 -e VUS=10 -e DURATION=10s k6 run /bench/gateway.k6.js
    if ($LASTEXITCODE -ne 0) { break }
}
```

基于模型代理冷启动、连接建立和初始化开销，初次请求可能显著更慢。
预热若出现阈值失败，查看原因并重新检查；不要通过放宽阈值掩盖未达到目标速率的问题。

## 固定并发

先测试完整链路，同时在途请求数为 10，持续 30 秒：

```powershell
docker compose -f deploy/docker-compose.benchmark.yml run --rm -e TARGET=full -e VUS=10 -e DURATION=30s k6 run /bench/gateway.k6.js
```

`VUS` 是虚拟用户数，每个用户串行发送一个请求，收到完整响应后再发下一个，无额外思考间隔。
`DURATION` 是发起负载的时长，结束后默认最多再等待 35 秒完成在途请求，因此总耗时可能更长。
把 TARGET 分别改为 mock、litellm、higress、full，在相同条件下比较。
根据机器余量逐级提高 VUS：10、20、50、100、200。每个档位重复三轮。

## 固定请求速率

固定并发在服务变慢时会自然降低请求速率。为了观察队列堆积，可用固定到达速率：

```powershell
docker compose -f deploy/docker-compose.benchmark.yml run --rm -e TARGET=full -e LOAD_MODE=rate -e RPS=50 -e VUS=20 -e MAX_VUS=200 -e DURATION=30s k6 run /bench/gateway.k6.js
```

这里目标为每秒发起 50 次请求，预分配 20 个用户，最多使用 200 个。
不是“并发 50”。`droppedIterations` 必须为 0，才能说明这轮目标请求确实按计划发起。
若发生漏发，检查 k6 资源、VUS 是否够用以及上游是否太慢；不能只看已完成请求的成功率。
短时间的边界调度可能使总请求数比 RPS * DURATION 略多或略少，以实际报告为准。

先依次检查 10、50、100、200 RPS，上一档无异常再提高。
吞吐不再增加而排队、P95 或错误率增加时，结合四路对照和资源指标定位瓶颈。
确认稳定区间后延长到 5-10 分钟，最终再做 30-60 分钟稳定性测试。

## 流式和响应大小

默认非流式立即返回 512 个 ASCII 字符。流式默认发送 10 个内容分片，间隔 50ms，
加上结束事件和 `[DONE]`。固定答案用于校验完整性；usage 是占位值，不能用来分析真实模型 token/s。

```powershell
docker compose -f deploy/docker-compose.benchmark.yml run --rm -e TARGET=full -e STREAM=1 -e VUS=20 -e DURATION=30s k6 run /bench/gateway.k6.js
```

流式校验完整文字、结束原因和 `[DONE]`。k6 的 HTTP 调用会等完整响应后返回，
本脚本测整个流的耗时，不测 TTFT。`http_req_waiting` 也不能直接当作首段文字延迟。

模拟较慢的上游、较大的回答和输入：

```powershell
$env:MOCK_DELAY_MS = '100'
$env:MOCK_RESPONSE_CHARS = '8192'
docker compose -f deploy/docker-compose.benchmark.yml up -d --wait --wait-timeout 180
docker compose -f deploy/docker-compose.benchmark.yml run --rm -e TARGET=full -e INPUT_CHARS=4096 -e RESPONSE_CHARS=8192 -e VUS=20 -e DURATION=30s k6 run /bench/gateway.k6.js
```

`RESPONSE_CHARS` 必须匹配模拟服务的 `MOCK_RESPONSE_CHARS`，否则内容校验失败。
可用 `MOCK_STREAM_CHUNKS` 和 `MOCK_STREAM_INTERVAL_MS` 调整流式负载，上游环境变量修改后需要重新 up。
长流还需同步调整 `TIMEOUT`、`GRACEFUL_STOP` 和业务延迟阈值 `P95_MS`。

恢复默认响应设置：

```powershell
Remove-Item Env:MOCK_DELAY_MS,Env:MOCK_RESPONSE_CHARS -ErrorAction SilentlyContinue
docker compose -f deploy/docker-compose.benchmark.yml up -d --wait --wait-timeout 180
```

## 看结果

JSON 报告自动保存到 `runtime/gateway-benchmark/reports`，默认文件名含目标、流模式和时间戳。
可以通过 `-e REPORT=/reports/my-run.json` 指定文件名；重复使用同名文件会覆盖该报告。

| 输出 | 含义 |
| --- | --- |
| requests | 实际完成的 HTTP 请求数 |
| validRate | HTTP 200 且返回完整预期内容的比例 |
| successfulRps | 每秒成功完成数，包含测试收尾时间的影响 |
| durationMs | HTTP 收发耗时分布，单位毫秒，含 P95；不含 DNS、建立连接等阶段 |
| blockedMs / connectingMs | 请求阻塞和连接建立耗时，帮助识别连接与客户端开销 |
| droppedIterations | 固定速率模式未能按计划发起的次数 |
| thresholds | 各项验收条件是否通过 |

默认阈值：有效响应 >=99%、HTTP 失败 <=1%、P95 <1000ms；固定速率模式另外要求零漏发。
这些只是示例门槛，不是 Higress/LiteLLM 的性能承诺。`-e P95_MS=200` 可修改延迟门槛，
成功率门槛可按业务要求在 k6 脚本中调整。未达门槛时 k6 返回非零退出码。

另开一个终端观察资源：

```powershell
docker stats
```

重点关注 api-gateway-benchmark 的 mock、litellm、higress 和 k6 容器，以及 Docker Desktop 整体 CPU、内存。
如果 mock 或 k6 已饱和，就不能将平台吞吐停滞归因于网关。
要得出正式容量结论，应把负载发生器、模拟上游和被测网关放到不同机器，
固定 CPU/内存配额、镜像版本、工作进程数、插件、日志、网络和连接协议，并记录实际资源占用。
当前镜像标签跟随原项目；正式结果应记录镜像 ID 或固定 digest，避免升级影响可比性。

模拟上游累计请求计数（包括预热，重启归零）：

```powershell
Invoke-RestMethod -Uri http://localhost:19000/metrics -Headers @{Authorization='Bearer sk-benchmark'}
```

它记录 started、completed、aborted、active、peak 和当前响应设置，可用于比较一轮前后的实际上游调用量。
LiteLLM 重试关闭时，正常非流式测试的新增上游调用数应与请求数对应。

不要简单相减两次 P95 就称为“网关增加的 P95 延迟”：不同轮的百分位不是同一批请求，
还受排队和资源争用影响。优先比较重复测试中的稳定趋势和资源指标。

## 停止与本次验证

```powershell
docker compose -f deploy/docker-compose.benchmark.yml down
```

只停止并移除这个独立测试项目的容器和网络，runtime 中的报告和配置保留。

本次已完成模拟上游的鉴权、固定 JSON、SSE 完整性、客户端取消释放的离线测试。
四条路径已进行小规模真实 Docker 链路检查，包含 JSON 和 SSE；另检查完整链路固定并发执行。
这些检查证明压测装置可用，未进行高负载或持续容量测量，不能据此报告网关最大 RPS。

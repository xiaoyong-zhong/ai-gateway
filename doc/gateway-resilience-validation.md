# 异常与并发验证

脚本：`test/test_gateway_resilience.py`。在项目根目录的 PowerShell 中执行，默认经 Higress 的 8080 端口。
使用 `GATEWAY_API_KEY`，未设置时使用本地测试密钥。每次先发送一个正常非流式基线请求，
基线失败则停止后续阶段。基线不计入并发统计，计入最终总测试数。
真实请求可能产生调用费用。优先使用已验证的聊天模型，不要填写图片生成或向量模型。

## 异常用例

```powershell
python -X utf8 test/test_gateway_resilience.py --model my-qwen3.6-27b --suite errors
```

| 用例 | 预期状态码 |
| --- | --- |
| 不传密钥 | 401 / 403 |
| 错误密钥 | 401 / 403 |
| 不存在的模型 | 400 / 404 |
| 缺少 model | 400 / 422 |
| 缺少 messages | 400 / 422 |
| messages 类型错误 | 400 / 422 |
| JSON 不完整 | 400 / 422 |
| 请求体为空 | 400 / 422 |

异常测试的 PASS 表示按预期拒绝了请求。5xx、错误请求被接受、连接失败或无响应都不算通过。
这些是本项目的验收标准，不代表所有供应商的原始接口都使用完全相同的错误码。
错误码正确后，还应检查错误信息是否清晰、是否泄露内部实现细节、是否发生了多余重试。

## 并发阶梯

先做单并发基准，再逐步增加同时在途请求数。每轮请求总数由 `--requests` 控制，
不是每个线程的请求数；例如并发 5、请求 20，是最多同时 5 个请求，总计 20 个。

```powershell
python -X utf8 test/test_gateway_resilience.py --model my-qwen3.6-27b --suite load --concurrency 1 --requests 10
python -X utf8 test/test_gateway_resilience.py --model my-qwen3.6-27b --suite load --concurrency 2 --requests 20
python -X utf8 test/test_gateway_resilience.py --model my-qwen3.6-27b --suite load --concurrency 5 --requests 20
python -X utf8 test/test_gateway_resilience.py --model my-qwen3.6-27b --suite load --concurrency 10 --requests 50
```

查看上一轮结果后再执行下一轮。出现持续 429、5xx 或延迟明显增加时，先定位原因。
默认提示词为 `Reply only OK.`，`--max-tokens` 默认 512；思考模型的预算可能还需要提高。
初始短回答适合测基本链路，正式容量验证还要覆盖实际业务的输入长度、输出长度和模型组合。

流式并发单独测：

```powershell
python -X utf8 test/test_gateway_resilience.py --model my-qwen3.6-27b --suite load --stream --concurrency 5 --requests 20
```

流式不在终端交错打印回答，检查非空文字、`finish_reason`、`[DONE]` 和 SSE 内容类型，
统计首段非空文字的到达时间。HTTP 200 后流中返回 error 或提前断开也计为失败。

## 如何看报告

每轮 JSON 报告自动保存在 `runtime/gateway-checks`，终端打印路径。退出码 0 表示本轮全部用例通过，1 表示存在失败。

| 字段 | 含义 |
| --- | --- |
| success_rate | 有效完成的请求数 / 并发阶段总请求数 |
| successful_rps | 成功请求数 / 并发阶段实际耗时 |
| completed_rps | 所有完成请求（包括失败）/ 实际耗时 |
| status_counts | 各状态码数量；None 表示未收到 HTTP 响应 |
| success_latency | 成功请求的平均、P50、P95、最大耗时 |
| all_latency | 包含失败请求在内的耗时统计 |
| success_first_text | 流式成功请求的首段文字延迟；非流式为 null |
| request_id | 响应中存在时记录 x-request-id 或 x-litellm-call-id，便于查日志 |

HTTP 200 但没有非空字符串 `message.content` 时，仍判为有效回答失败。此时终端和报告记录
`response_diagnostics`：内容类型/长度、结束原因、推理字段长度、工具调用数量和返回的 token 用量，
不保存回答、推理正文或工具参数。`finish_reason=length` 提示检查输出预算；`stop` 本身不能证明预算不足。
HTTP 状态码全部为 200、成功率却小于 100% 时，应查看这些内容校验失败记录。

客户端没有自动重试，服务端 LiteLLM 或供应商可能仍有重试。
单次延迟从工作线程开始请求时计算，不包括线程池排队；整轮耗时包括调度和输出开销。
这是固定并发、完成一个再发下一个的负载，不能当作固定 RPS 压测。
P50/P95 使用 nearest-rank 方法，小样本的 P95 常等于最大值，只能作为冒烟数据。
脚本不自动判断延迟是否达标，应按业务预先确定成功率、P95 和吞吐量目标。

## 还需要故障注入的项目

以下项目没有被上述自动测试覆盖，需在独立测试路由或测试环境完成：

1. **限流**：先明确规则属于哪一层、按哪个账号/模型计数、阈值和窗口。让负载超过阈值，
   检查 429、错误说明以及提供时的 Retry-After；窗口恢复后重新调用应成功。
   出现 429 不能单凭状态码判断来自 Higress、LiteLLM 还是上游，需要对照日志。
2. **上游不可达**：添加独立测试模型，让它指向测试用不可达服务，发起请求。检查在预期时间内
   返回可解释的错误、重试次数受控、其他模型仍可用；恢复该测试上游后调用恢复。
3. **上游慢响应**：使用能控制延迟的测试上游，让响应慢于网关或 LiteLLM 配置的服务端超时。
   客户端超时设得比服务端超时更长，才能观察网关返回的超时错误。只把脚本 `--timeout` 调小，
   验证的是客户端等待超时，不能证明服务端正确超时或释放资源。
4. **流式中断与取消**：测试上游发送数个 SSE 事件后断开，客户端应判定失败；另测客户端主动关闭连接，
   在网关和上游日志/指标中确认请求被取消、连接/并发占用释放。仅看到客户端退出不能证明上游停止计算。
5. **故障恢复**：恢复测试上游后再次执行基本请求和小并发，观察是否有连接泄漏、请求堆积或持续失败。

日志查看：

```powershell
docker compose -f deploy/docker-compose.yml logs --since 10m litellm
docker compose -f deploy/docker-compose.yml logs --since 10m higress
```

不要把关闭整个正常服务当作首选测试方式；优先隔离故障路由，避免干扰正在进行的其他测试。

## 2026-09-10 实测

- 模型：`my-qwen3.6-27b`。
- 异常用例 8 项中 7 项状态码符合预期。
- 错误密钥返回 `400`、`No connected db.`，不是预期的 `401/403`。请求被拒绝，但鉴权错误语义需修正。
- messages 类型错误虽然返回 400，耗时约 4.78 秒，错误中出现内部异常和 `LiteLLM Retried: 2 times`，
  建议继续检查参数校验是否过晚以及无效请求重试。
- 非流式并发 2、总请求 4：全部成功，平均 2.81 秒，P95 3.41 秒，成功吞吐约 0.68 请求/秒。
- 流式并发 2、总请求 2：全部成功，平均 2.95 秒，首段文字平均 2.84 秒。
- 首次使用 128 输出 token 的基线出现 HTTP 200 但无最终文字；已将默认预算调到 512，并在错误信息中提示检查预算。
- 以上仅为小样本冒烟，未验证大并发容量、限流阈值、上游故障注入或资源释放。

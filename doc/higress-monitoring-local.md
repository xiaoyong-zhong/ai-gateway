# 本地 Higress 监控

在 Higress 控制台的“监控面板 → 监控页面 URL”填写：

```text
http://localhost:3000/d/higress-local?orgId=1&kiosk
```

点击“确定”。Grafana 已配置匿名只读查看和页面嵌入，浏览器在本机使用时无需登录。
Grafana 端口仅绑定 127.0.0.1；其他电脑不能使用这个 localhost 地址访问。

## 启动与恢复

在项目根目录执行：

```powershell
docker compose -p api-gateway-monitoring -f docker-compose.monitoring.yml up -d
```

网关容器必须已经存在。独立监控 Prometheus 共享网关网络空间，以访问仅绑定回环地址的 Envoy 管理指标接口。没有对宿主机开放管理接口。
如果重建了 api-gateway-higress 容器，执行以下命令重新关联：

```powershell
docker compose -p api-gateway-monitoring -f docker-compose.monitoring.yml up -d --force-recreate prometheus
```

## 实际采集和看板

- 本次使用独立 Prometheus（容器名 api-gateway-monitoring-prometheus），在网关网络空间内监听 19090，保留 7 天数据。
- 原有 localhost:9090 的 Prometheus 保持原样，不是这个看板的数据源。
- Grafana 数据源 UID 为 higress-prometheus，地址为 http://api-gateway-higress:19090。
- higress 任务采集 127.0.0.1:15000/stats/prometheus，包含 Envoy 网关指标。
- higress-agent 任务采集 127.0.0.1:15020/stats/prometheus。目前该接口合并 Envoy 指标失败，只提供 agent 指标，因此不能只采集这个端口。
- 看板配置保存在 config/monitoring/dashboards/higress.json，自动导入，每 15 秒刷新。
- 入口仅统计 `outbound_0.0.0.0_8080` 和 `outbound_0.0.0.0_8443`，排除 `stats`、`admin`、`agent`。累计数包含模型调用、模型列表查询、鉴权失败和其他业务端口 HTTP 请求，不等于模型推理次数。
- 上游请求、超时、重试和耗时仅统计 `outbound|80||litellm.static` 与 `outbound|80||llm-litellm.internal.static`，两者都指向 `172.30.50.2:4000`。排除 `prometheus_stats` 等内部服务；新增上游服务或业务监听端口时需同步更新看板筛选。
- 当前看板不包含 LiteLLM 费用、逐 Key 统计或模型首字时间。
- 无流量时，耗时面板可能无数据；速率至少需要两个采样点。采集 UP 不等于模型 API 正常。

## 用一个模型请求观察指标

在项目根目录运行（真实模型调用，会产生用量）：

```powershell
python -X utf8 test/test_higress_route.py --stream --prompt "请用一句话介绍自己"
```

脚本使用顶部 `GATEWAY_API_KEY` 中的 Higress 消费者 Key，默认向 `http://localhost:8080/v1/chat/completions` 发送一次请求，携带 `Host: ai-test.local`，模型为 `my-qwen3.6-27b`。无需第三方 Python 依赖。成功时输出 HTTP 200、响应模型、Usage、正文和 PASS；仅 HTTP 200 不代表正文完整。

记录业务入口累计数，运行脚本，再等待约 15–30 秒刷新看板。无其他业务请求时入口累计数增加 1；专用 AI 路由对应上游图例 `outbound|80||llm-litellm.internal.static`。鉴权失败仍计入入口，上游计数取决于请求是否转发；重试可能使上游次数多于入口次数。短请求的活跃状态可能被 15 秒采样间隔错过。

## 2026-09-14 初次部署验证与当时故障（历史记录）

Grafana 健康检查正常，两个采集任务 UP，12 个面板查询均通过 Prometheus 语法和执行检查。
没有发起付费模型请求；尚未验证恢复流量后的延迟、重试图表。

当前业务入口 8080 返回空响应，Envoy /ready 返回 INITIALIZING。
网关日志反复报告 http://localhost:8002/plugins/key-auth/1.0.0/plugin.wasm 返回 404。
需要恢复鉴权插件并确认网关就绪，才能继续真实模型调用验证。不要通过关闭鉴权来掩盖此故障。

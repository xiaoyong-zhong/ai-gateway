# 校园统一 AI 网关

一个面向校园场景的 OpenAI 兼容 AI 网关集成项目。项目将 **Higress** 与 **LiteLLM** 组合成两层网关：Higress 处理统一入口、路由和边缘治理，LiteLLM 负责模型别名、上游供应商适配、虚拟密钥、用量记录和模型级权限。

当前仓库以本地集成验证和部署配置为主，包含可直接启动的 Docker Compose 环境、LiteLLM Responses 兼容补丁、监控配置、功能测试和独立压测环境。

## 能力概览

- 统一提供 `/v1/chat/completions`、`/v1/responses`、`/v1/embeddings` 等 OpenAI 兼容接口。
- Higress 暴露 HTTP/HTTPS 网关入口，负责域名路由、WASM 鉴权插件和 AI 代理能力。
- LiteLLM 将多个模型供应商映射为稳定的内部模型别名，并记录用量。
- PostgreSQL 保存 LiteLLM 的密钥、团队、用量和审计相关数据。
- 提供 Responses 流式空 `choices` 兼容补丁，以及 Qwen 系统/开发者消息适配回调。
- Prometheus + Grafana 采集 Higress/Envoy 指标并提供本地仪表盘。
- 压测环境使用独立 Docker 网络和本地 mock 上游，不会调用真实模型供应商。

## 架构

```text
客户端 / SDK
    │
    ▼
Higress :8080/:8443       入口、路由、鉴权、WASM 插件、Envoy 指标
    │
    ▼
LiteLLM :4000             模型别名、供应商适配、虚拟 Key、用量记录
    │
    ├── PostgreSQL 16      持久化 LiteLLM 数据
    └── 上游模型供应商      DashScope、校园 AIGC、DeepSeek、Agnes 等

Prometheus :19090 ──► Grafana http://localhost:3000
```

| 组件 | 地址/端口 | 用途 |
| --- | --- | --- |
| Higress | `localhost:8080`、`:8443`、`:8001` | 网关 HTTP、HTTPS、控制台 |
| LiteLLM | `localhost:4000` | AI 代理和 OpenAI 兼容 API |
| PostgreSQL | 仅容器网络 | LiteLLM 持久化数据 |
| Grafana | `localhost:3000` | 监控仪表盘 |
| Prometheus | `localhost:19090` | 指标查询 |

## 快速开始

### 前置条件

- Docker Desktop（含 Docker Compose v2）
- Python 3.11+（运行测试脚本）
- Node.js 24+（运行 mock 压测服务的离线测试）
- 可访问配置中所需的上游模型 API

### 配置密钥

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，至少配置实际使用模型对应的供应商密钥，例如 `DASHSCOPE_API_KEY`、`ZHILIN_aigc_API_KEY`、`DEEPSEEK_API_KEY` 或 `AGNES_API_KEY`。`.env` 已被 Git 忽略，请勿提交真实密钥。

LiteLLM 主密钥在 `docker-compose.yml` 中默认是本地测试值 `sk-local-test`。生产环境应替换为安全密钥，并同步调整调用方配置。

### 启动主环境

首次启动前创建 PostgreSQL 数据卷：

```powershell
docker volume create litellm_postgres_data
docker compose up -d --build
docker compose ps
```

检查服务：

```powershell
Invoke-WebRequest -UseBasicParsing http://localhost:4000/health/liveliness
Invoke-WebRequest -UseBasicParsing http://localhost:4000/v1/models -Headers @{ Authorization = 'Bearer sk-local-test' }
```

LiteLLM 控制台：`http://localhost:4000/ui/`；Higress 控制台：`http://localhost:8001`。

### 发送一次请求

```powershell
$body = @{
  model = 'my-qwen3.6-27b'
  messages = @(@{ role = 'user'; content = '请用一句话介绍 API 网关' })
  temperature = 0
  max_tokens = 64
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
  -Uri http://localhost:8080/v1/chat/completions `
  -Method Post `
  -Headers @{ Authorization = 'Bearer sk-local-test' } `
  -ContentType 'application/json' `
  -Body $body
```

直接访问 LiteLLM 时，将 URL 改为 `http://localhost:4000/v1/chat/completions`。

## 模型配置

模型别名和上游地址位于 [`config/litellm.yaml`](config/litellm.yaml)。当前配置包含 DashScope、校园 AIGC、DeepSeek 和 Agnes AI 的示例。修改后重启：

```powershell
docker compose up -d --build litellm
```

上游 API Key 通过 `os.environ/...` 引用环境变量，不要把密钥直接写入 YAML。LiteLLM 的虚拟 Key、团队和预算策略通过管理接口或控制台配置。

## Responses 兼容补丁

`config/litellm-patch/` 提供自定义 LiteLLM 镜像和回调，主要处理流式空 `choices` 的 `IndexError`，并为指定 Qwen 模型合并前置文本型 system/developer 指令。构建：

```powershell
docker compose build litellm
docker compose up -d --no-deps --no-build litellm
```

详细范围、限制和回滚方式见 [`config/litellm-patch/README.md`](config/litellm-patch/README.md)。

## 监控

```powershell
docker compose -f docker-compose.monitoring.yml up -d
```

- Grafana：<http://localhost:3000>
- Prometheus：<http://localhost:19090>

监控配置位于 [`config/monitoring`](config/monitoring)。Prometheus 从 Higress 的 `127.0.0.1:15000` 和 agent 的 `127.0.0.1:15020` 抓取指标。

## 测试与验证

离线单元测试：

```powershell
python -B -m pytest -p no:cacheprovider test/test_dual_gateway_unit.py test/test_gateway_resilience_unit.py -q
node --test test/benchmark/mock.test.mjs
```

常用集成脚本：

```powershell
$env:GATEWAY_API_KEY = '你的网关密钥'
python -X utf8 test/test_dual_gateway.py --key $env:GATEWAY_API_KEY
python -X utf8 test/test_higress_route.py
```

`test_responses_patch_live.py` 会访问真实上游并可能产生费用，运行前请确认模型和测试 Key。

## 隔离压测

压测使用独立的 `172.30.51.0/24` 网络和 mock 上游：

```powershell
docker compose -f docker-compose.benchmark.yml up -d --wait --wait-timeout 180
docker compose -f docker-compose.benchmark.yml run --rm `
  -e TARGET=full -e VUS=10 -e DURATION=30s k6 run /bench/gateway.k6.js
```

目标可选 `mock`、`litellm`、`higress`、`full`；报告写入 `runtime/gateway-benchmark/reports`。详见 [`doc/gateway-capacity-benchmark.md`](doc/gateway-capacity-benchmark.md)。

## 目录说明

```text
config/                 LiteLLM、监控和自定义补丁配置
doc/                    架构、部署、验证和压测文档
runtime/                Higress 运行时配置及测试报告
sources/higress-src/    Higress 源码快照
sources/litellm-src/    LiteLLM 源码快照
test/                   功能、韧性、路由和压测脚本
docker-compose.yml      主环境
docker-compose.monitoring.yml 监控环境
docker-compose.benchmark.yml  隔离压测环境
```

## 停止与清理

```powershell
docker compose down
docker compose -f docker-compose.monitoring.yml down
docker compose -f docker-compose.benchmark.yml down -v
```

主环境停止时默认保留 `litellm_postgres_data`。确认无需数据后再执行 `docker volume rm litellm_postgres_data`。

## 已知边界

- 根 Compose 面向本地集成验证，未提供生产级 TLS 证书、外部密钥管理和高可用编排。
- 费用统计依赖上游 usage 和价格映射；`spend=0` 不能单独证明调用免费。
- Higress 只能观测经过它的请求；直接访问 LiteLLM 的流量不会出现在 Higress 指标中。
- 压测 mock 响应用于验证网关链路和脚本，不代表真实模型吞吐、延迟或费用表现。

## 相关文档

- [`doc/architecture.md`](doc/architecture.md)：系统架构和请求链路
- [`doc/AI网关完整技术链路与架构设计.md`](doc/AI网关完整技术链路与架构设计.md)：完整技术链路
- [`doc/项目现状核查-验证记录-2026-09-16.md`](doc/项目现状核查-验证记录-2026-09-16.md)：现状和验证记录
- [`doc/gateway-resilience-validation.md`](doc/gateway-resilience-validation.md)：韧性验证
- [`doc/higress-litellm-evaluation.md`](doc/higress-litellm-evaluation.md)：Higress/LiteLLM 评估

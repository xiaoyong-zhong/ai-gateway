# 校园统一 AI 网关 — 架构文档

**版本：** 1.0（中文版）
**日期：** 2026-09-16
**项目根目录：** `d:\zhilin\API网关\`
**状态：** 本地集成与验证阶段（尚未交付生产）

---

## 目录

1. [系统概览](#1-系统概览)
2. [架构图](#2-架构图)
3. [组件职责表](#3-组件职责表)
4. [请求链路 — 对话补全 API](#4-请求链路--对话补全-api)
5. [配置链路](#5-配置链路)
6. [数据流分析](#6-数据流分析)
7. [部署与启动顺序](#7-部署与启动顺序)
8. [Higress 与 LiteLLM 职责对比](#8-higress-与-litellm-职责对比)
9. [具体请求示例](#9-具体请求示例)
10. [网络拓扑](#10-网络拓扑)
11. [安全模型](#11-安全模型)
12. [已知缺口与风险](#12-已知缺口与风险)

---

## 1. 系统概览

校园统一 AI 网关是一个分层 API 网关系统，为多个上游大语言模型提供商提供统一的 OpenAI 兼容入口。系统由两层网关组成：

- **Higress**（Apache 孵化项目）—— 边缘入口层。基于 Envoy 的 API 网关，负责 TLS 终止、域名路由、密钥认证、流量治理、速率限制、WAF 能力以及连接池管理。
- **LiteLLM** —— AI 模型路由层。Python 代理，将各提供商专有 API 统一翻译为 OpenAI 兼容接口，管理虚拟密钥、执行每用户/团队预算限制、实现跨提供商故障转移，并记录 Token 用量与成本。

后端由 **PostgreSQL 16** 提供持久化存储，**Prometheus + Grafana** 提供运维可观测性。项目还包含自定义 Patch 套件，修复了 LiteLLM 流式迭代器中空 choices 导致的 `IndexError` 崩溃，并合并了 Qwen 聊天模板的系统消息。

### 上游模型提供商

| 提供商 | 端点 | 服务模型 |
|---|---|---|
| DashScope（阿里云） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | kimi-k2.7-code, qwen3.5-ocr, qwen3.7-text-embedding |
| 智临校园 AIGC | `https://test2-aigc.campusapp.com.cn/api/v1` | Qwen3.6-27B |
| DeepSeek | `https://api.deepseek.com/` | deepseek-v4-flash |
| Agnes AI | `https://apihub.agnes-ai.com/v1` | agnes-2.5-pro, agnes-image-2.5-flash |

---

## 2. 架构图

```mermaid
graph TB
    subgraph CLIENT["客户端"]
        APP1["校园应用"]
        APP2["研究工具"]
        APP3["开发者 SDK"]
    end

    subgraph EDGE["边缘层 — Higress（172.30.50.3）"]
        HIGRESS["Higress 网关<br/>Envoy 数据面<br/>:8080 HTTP / :8443 HTTPS / :8001 控制台"]
        KEYAUTH["WASM：key-auth<br/>(优先级 310, AUTHN 阶段)"]
        AIPROXY["WASM：ai-proxy<br/>(优先级 100)"]
        AISTATS["WASM：ai-statistics<br/>(优先级 900)"]
        MCPPROXY["McpBridge 服务注册<br/>静态服务发现"]
    end

    subgraph AI["AI 路由层 — LiteLLM（172.30.50.2）"]
        LITELLM["LiteLLM 代理<br/>自定义 Patch 镜像<br/>:4000"]
        STREAMPATCH["流式补丁<br/>3 处空 choices 防护"]
        CALLBACK["QwenResponsesCompatibility<br/>回调前置钩子"]
    end

    subgraph DATA["数据层"]
        PG[(PostgreSQL 16<br/>172.30.50.4:5432<br/>litellm 数据库)]
    end

    subgraph MON["监控"]
        PROM["Prometheus<br/>与 Higress 共享命名空间<br/>采集 :15000 / :15020"]
        GRAF["Grafana 12.1.1<br/>172.30.50.5:3000<br/>12 面板仪表板"]
    end

    subgraph PROVIDERS["上游模型提供商"]
        DASH["DashScope<br/>(阿里云)"]
        ZHILIN["智临 AIGC<br/>(校园)"]
        DEEP["DeepSeek"]
        AGNES["Agnes AI"]
    end

    APP1 -->|"POST /v1/chat/completions"| HIGRESS
    APP2 -->|"POST /v1/chat/completions"| HIGRESS
    APP3 -->|"POST /v1/embeddings"| HIGRESS

    HIGRESS --> KEYAUTH
    KEYAUTH --> AIPROXY
    AIPROXY --> MCPPROXY
    MCPPROXY -->|"litellm.static → 172.30.50.2:4000"| LITELLM

    LITELLM --> STREAMPATCH
    LITELLM --> CALLBACK
    LITELLM -->|"提供商 API 密钥"| PG

    LITELLM -->|"OpenAI 兼容"| DASH
    LITELLM -->|"OpenAI 兼容"| ZHILIN
    LITELLM -->|"OpenAI 兼容"| DEEP
    LITELLM -->|"OpenAI 兼容"| AGNES

    LITELLM -.->|"消费日志、用量"| PG

    HIGRESS --> AISTATS
    AISTATS -->|"Token 指标写入访问日志"| HIGRESS

    HIGRESS -.->|"Envoy 指标 127.0.0.1:15000"| PROM
    HIGRESS -.->|"Agent 指标 127.0.0.1:15020"| PROM
    PROM -->|"HTTP :19090"| GRAF

    classDef gateway fill:#4a90d9,color:#fff,stroke:#2c5f8a
    classDef ai fill:#7b68ee,color:#fff,stroke:#4a3ab5
    classDef data fill:#6a6,color:#fff,stroke:#3a3a3a
    classDef mon fill:#e67e22,color:#fff,stroke:#a0511b
    classDef provider fill:#95a5a6,color:#fff,stroke:#607d8b
    classDef client fill:#f5f5dc,color:#333,stroke:#999
    classDef wasm fill:#e74c3c,color:#fff,stroke:#a0211a

    class HIGRESS,gateway
    class KEYAUTH,AIPROXY,AISTATS,wasm
    class LITELLM,STREAMPATCH,CALLBACK,ai
    class PG,data
    class PROM,GRAF,mon
    class DASH,ZHILIN,DEEP,AGNES,provider
    class APP1,APP2,APP3,client
```

---

## 3. 组件职责表

| 组件 | 容器名 | 镜像 | 宿主机端口 | 内部 IP | 主要职责 |
|---|---|---|---|---|---|
| **Higress 网关** | `api-gateway-higress` | `higress/all-in-one:latest` | 8080, 8443, 8001 | 172.30.50.3 | 边缘入口：域名路由、TLS 终止、密钥认证（WASM）、流量治理、速率限制、WAF、连接池、Envoy 指标导出 |
| **LiteLLM 代理** | `api-gateway-litellm` | `api-gateway-litellm:responses-empty-choices-v1`（自定义 Patch 镜像） | 4000 | 172.30.50.2 | AI 模型路由：8 个模型别名、协议适配、虚拟密钥管理、消费/Token 记账、跨提供商故障转移、重试逻辑 |
| **PostgreSQL** | `litellm_db` | `postgres:16` | 无（仅内网） | 172.30.50.4 | 持久化存储：用户、团队、API 密钥、消费日志、Token 预算、审计记录、组织数据 |
| **Prometheus** | `api-gateway-monitoring-prometheus` | `prom/prometheus:latest` | 无（共享命名空间） | 与 Higress 共享 NS | 指标采集：每 15 秒抓取 Envoy 数据面（`:15000`）和 Higress Agent（`:15020`），保留 7 天 |
| **Grafana** | `api-gateway-grafana` | `grafana/grafana:12.1.1` | 127.0.0.1:3000 | 172.30.50.5 | 可视化：12 面板仪表板（采集健康、运行时长、QPS、状态码、5xx 比例、上游速率/超时/重试/延迟、监听器状态、活跃请求） |

### Higress WASM 插件

| 插件 | 文件路径 | 优先级 | 阶段 | 生效范围 | 职责 |
|---|---|---|---|---|---|
| **key-auth** | `runtime/higress/wasmplugins/key-auth.internal.yaml` | 310 | AUTHN | 仅 `ai-test.local` 入口（默认入口已禁用） | 通过 `Authorization: Bearer <token>` 头验证消费者密钥。仅允许 `test-client` 消费者。 |
| **ai-proxy** | `runtime/higress/wasmplugins/ai-proxy.internal.yaml` | 100 | — | `llm-litellm.internal.static` 服务 | 重写目标地址为 `http://172.30.50.2:4000/v1`，将客户端 Authorization 替换为 LiteLLM 主密钥 `sk-local-test` |
| **ai-statistics** | `runtime/higress/wasmplugins/ai-statistics-2.0.1.yaml` | 900 | — | 仅 `ai-test.local` 入口 | 解析响应中的 Token 用量指标，通过 `ai_log` 过滤器状态注入 Envoy 访问日志 |

### LiteLLM 自定义 Patch

| Patch | 文件路径 | 职责 |
|---|---|---|
| **流式迭代器防护** | `config/litellm-patch/patch_streaming.py` | 对 `streaming_iterator.py` 进行 3 处精确修改：在 `_ensure_output_item_for_chunk` 中跳过空 choices 数据块、在 `_is_reasoning_end` 中对空 choices 返回 False、在文本增量提取中对空 choices 返回空字符串 |
| **Qwen Responses 回调** | `config/litellm-patch/gateway_responses_compat.py` | `QwenResponsesCompatibility` 回调类；在模型 `my-qwen3.6-27b` 的 `aresponses` 调用类型下激活；将开头的 `system`/`developer` 消息合并到 `instructions` 字段，避免 Qwen 聊天模板拒绝重复系统消息 |

---

## 4. 请求链路 — 对话补全 API

### 4.1 入口路径

系统支持两条不同的入口路径，认证模型各异：

| 方面 | 路径 A — 标准入口 | 路径 B — AI 路由入口 |
|---|---|---|
| **Host 头** | 默认（无或 `higress-default-domain`） | `ai-test.local` |
| **Higress Ingress** | `litellm-proxy.yaml`（路径 `/v1` Prefix） | `ai-route-litellm-ai-test.internal.yaml`（路径 `/v1` Prefix） |
| **Higress 认证** | 已禁用（`configDisable: true`） | 已启用（key-auth 验证消费者密钥） |
| **客户端发送** | LiteLLM 虚拟密钥（`sk-xxx`） | Higress 消费者密钥（`3f8f9e70-...`） |
| **LiteLLM 接收** | 客户端虚拟密钥（透明转发） | 主密钥 `sk-local-test`（由 ai-proxy 重写） |
| **重试/故障转移** | 未配置 | 3 次尝试、120 秒超时、错误/超时/非幂等时重试 |
| **统计** | 无 | ai-statistics 插件捕获 Token 指标 |

### 4.2 时序图 — 路径 A（标准入口）

```mermaid
sequenceDiagram
    autonumber
    participant 客户端
    participant Higress as Higress（:8080）
    participant LiteLLM as LiteLLM（:4000）
    participant PG as PostgreSQL
    participant 提供商 as 上游模型提供商

    客户端->>Higress: POST /v1/chat/completions<br/>Authorization: Bearer sk-xxx<br/>Content-Type: application/json
    Note over Higress: Ingress: litellm-proxy.yaml<br/>key-auth: 已禁用（configDisable: true）<br/>路由: /v1 Prefix → litellm.static:80
    Higress->>LiteLLM: 转发请求（认证头不变）

    LiteLLM->>PG: 验证虚拟密钥 sk-xxx<br/>检查团队权限、预算、过期时间
    PG-->>LiteLLM: 密钥有效，模型允许

    LiteLLM->>LiteLLM: 模型查找: my-qwen3.6-27b → openai/Qwen3.6-27B<br/>端点: test2-aigc.campusapp.com.cn
    Note over LiteLLM: 回调: gateway_responses_compat<br/>（对 /chat/completions 无操作<br/>仅对 /responses 生效）

    LiteLLM->>提供商: POST /api/v1/chat/completions<br/>Authorization: Bearer ZHILIN_aigc_API_KEY
    提供商-->>LiteLLM: JSON 响应或 SSE 流

    alt 非流式
        LiteLLM->>PG: 记录消费日志（用量、成本）
        LiteLLM-->>Higress: JSON 响应
    else 流式（SSE）
        LiteLLM->>LiteLLM: 应用流式补丁<br/>（跳过空 choices 数据块）
        LiteLLM-->>Higress: SSE 流透传
        LiteLLM->>PG: 流结束后记录消费日志
    end

    Higress-->>客户端: 响应（Server: envoy 头可见）
```

### 4.3 时序图 — 路径 B（AI 路由入口）

```mermaid
sequenceDiagram
    autonumber
    participant 客户端
    participant Higress as Higress（:8080）
    participant KeyAuth as key-auth WASM（310）
    participant AIProxy as ai-proxy WASM（100）
    participant LiteLLM as LiteLLM（:4000）
    participant PG as PostgreSQL
    participant 提供商 as 上游模型提供商
    participant AIStats as ai-statistics WASM（900）

    客户端->>Higress: POST /v1/chat/completions<br/>Host: ai-test.local<br/>Authorization: Bearer 3f8f9e70-...

    Note over Higress: Ingress: ai-route-litellm-ai-test.internal.yaml<br/>Host: ai-test.local, 路径: /v1 Prefix

    Higress->>KeyAuth: AUTHN 阶段: 验证消费者密钥
    KeyAuth-->>Higress: 允许（test-client 在白名单内）

    Higress->>AIProxy: AI-PROXY 阶段: 重写目标地址
    AIProxy-->>Higress: 设置 Authorization: Bearer sk-local-test<br/>设置目标: http://172.30.50.2:4000/v1

    Higress->>LiteLLM: POST /v1/chat/completions<br/>Authorization: Bearer sk-local-test

    LiteLLM->>PG: 验证主密钥 sk-local-test
    PG-->>LiteLLM: 密钥有效

    LiteLLM->>LiteLLM: 从请求体查找模型
    LiteLLM->>提供商: 转发至上游提供商

    提供商-->>LiteLLM: 响应
    LiteLLM-->>Higress: 响应

    Higress->>AIStats: STATISTICS 阶段: 解析 Token 用量
    AIStats-->>Higress: 注入 ai_log 访问日志字段

    Higress-->>客户端: 响应

    alt 上游故障
        Higress->>Higress: proxy-next-upstream 重试<br/>（3 次尝试、120 秒、错误/超时/非幂等）
    end
```

---

## 5. 配置链路

### 5.1 配置如何流入各服务

#### Docker Compose 入口

```
docker-compose.yml
├── db 服务
│   ├── environment: POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD
│   └── volume: litellm_postgres_data（命名卷，外部卷）
│
├── litellm 服务
│   ├── command: --config /app/config.yaml --port 4000
│   ├── environment: LITELLM_MASTER_KEY=sk-local-test, DATABASE_URL
│   ├── env_file: .env（可选；提供商 API 密钥）
│   ├── volume（只读）: config/litellm.yaml → /app/config.yaml
│   └── build: config/litellm-patch/Dockerfile
│       ├── COPY gateway_responses_compat.py → /app/
│       ├── RUN patch_streaming.py（修改 streaming_iterator.py）
│       └── RUN test_empty_choices.py + test_gateway_responses_compat.py
│
└── higress 服务
    ├── volumes: runtime/higress/ → /data（读写）
    └── depends_on: litellm（仅容器启动顺序）
```

#### Higress 配置树（运行时数据卷）

Higress 使用 Kubernetes 风格的控制中心，YAML 文件存储在 `/data`（挂载自 `runtime/higress/`）。Higress 控制器监控这些文件并将其翻译为 Envoy xDS 配置。

```
runtime/higress/
├── configmaps/
│   ├── higress-config.yaml           # 全局网关配置（缓冲区限制、超时、访问日志格式）
│   ├── higress-https.yaml            # TLS/ACME 设置（automaticHttps: true）
│   ├── domain-higress-default-domain.yaml   # 默认域名配置
│   ├── domain-ai-test.local.yaml                # ai-test.local 域名（HTTPS 关闭）
│   ├── ai-route-litellm-ai-test.yaml           # AI 路由 JSON 配置（上游、认证、重试策略）
│   ├── higress-console.yaml                    # 控制台设置
│   └── ...（CA 证书、CRL）
│
├── ingresses/
│   ├── default.yaml                                   # / → Higress 控制台（路径重写 /landing）
│   ├── litellm-proxy.yaml                             # /v1 Prefix → litellm.static:80
│   └── ai-route-litellm-ai-test.internal.yaml         # ai-test.local /v1 → llm-litellm.internal.static:80（含重试）
│
├── mcpbridges/
│   └── default.yaml        # 静态服务注册: litellm → 172.30.50.2:4000, llm-litellm.internal → 172.30.50.2:4000
│
├── wasmplugins/
│   ├── key-auth.internal.yaml            # 消费者密钥认证（仅 test-client，ai-test.local）
│   ├── ai-proxy.internal.yaml            # 认证头重写、目标路由
│   └── ai-statistics-2.0.1.yaml          # Token 用量日志
│
├── envoyfilters/
│   ├── higress-http-resolver-cluster.yaml          # HTTP 解析器（127.0.0.1:8889）
│   └── higress-gateway-global-custom-response.yaml  # 自定义响应处理
│
├── services/
│   └── higress-gateway.yaml     # 网关服务定义
│
└── secrets/
    ├── default.yaml              # 默认 TLS 密钥
    └── higress-console.yaml      # 控制台认证密钥
```

#### LiteLLM 配置

```
config/litellm.yaml
├── model_list（8 个条目）
│   ├── my-kimi-k2.7-code          → openai/kimi-k2.7-code          @ DashScope
│   ├── my-qwen3.5-ocr             → openai/qwen3.5-ocr             @ DashScope
│   ├── qwen3.7-flash              → openai/qwen3.5-ocr             @ DashScope（别名）
│   ├── my-qwen3.7-text-embedding  → openai/qwen3.7-text-embedding  @ DashScope
│   ├── my-qwen3.6-27b             → openai/Qwen3.6-27B            @ 智临 AIGC
│   ├── my-deepseek-v4-flash       → openai/deepseek-v4-flash       @ DeepSeek
│   ├── my-agnes-2.5-pro           → openai/agnes-2.5-pro           @ Agnes AI
│   └── my-agnes-image-2.5-flash   → openai/agnes-image-2.5-flash   @ Agnes AI
│
├── general_settings
│   └── master_key: os.environ/LITELLM_MASTER_KEY
│
├── litellm_settings
│   ├── drop_params: true
│   └── callbacks: [gateway_responses_compat.callback]
│
└── .env（可选，容器启动时加载）
    ├── DASHSCOPE_API_KEY
    ├── ZHILIN_aigc_API_KEY
    ├── DEEPSEEK_API_KEY
    └── AGNES_API_KEY
```

### 5.2 配置热重载机制

| 组件 | 重载方式 | 说明 |
|---|---|---|
| **Higress** | xDS 热重载 | 控制器监控 `runtime/higress/` 目录，YAML 变更触发 xDS 推送至 Envoy，无需重启 |
| **LiteLLM config.yaml** | 需重启容器 | 只读挂载，编辑 `config/litellm.yaml` 后需 `docker compose restart litellm` |
| **LiteLLM Patch** | 需重建镜像 | `patch_streaming.py` 在构建时运行，需 `docker compose build litellm` 后重启 |
| **LiteLLM 回调** | 需重建镜像 | `gateway_responses_compat.py` 在构建时 COPY 进镜像，需重建后重启 |
| **Prometheus** | 文件监控 + SIGHUP | `config/monitoring/prometheus.yml` 磁盘变更时自动重载 |
| **Grafana 仪表板** | 每 10 秒自动重载 | 配置目录扫描变更，数据源和仪表板 JSON 重新读取 |
| **PostgreSQL** | 需重建容器 | 环境变量在容器创建时设置，变更需完全重建 |

---

## 6. 数据流分析

### 6.1 用户数据流

```mermaid
graph LR
    subgraph IN["入站"]
        REQ["客户端请求<br/>model、messages、temperature、max_tokens"]
        AUTH["Authorization 头<br/>sk-xxx 或消费者密钥"]
    end

    subgraph GATEWAY["Higress 处理"]
        ROUTE["主机 + 路径匹配<br/>→ Ingress 选择"]
        AUTHN["密钥验证<br/>（仅路径 B）"]
        REWRITE["认证头重写<br/>（路径 B: ai-proxy）"]
    end

    subgraph PROXY["LiteLLM 处理"]
        KEYVAL["虚拟密钥 / 主密钥验证"]
        MODELR["模型别名 → 上游映射"]
        CBHOOK["回调前置钩子<br/>（仅 /responses）"]
        FORWARD["提供商 API 翻译"]
    end

    subgraph STORE["持久化"]
        SPEND["消费日志条目<br/>tokens、成本、模型、密钥"]
        AUDIT["审计日志<br/>（启用时）"]
    end

    subgraph OUT["出站"]
        RESP["JSON / SSE 流响应"]
        TOKENS["Token 用量元数据"]
    end

    REQ --> ROUTE --> AUTHN --> REWRITE --> KEYVAL --> MODELR --> CBHOOK --> FORWARD
    AUTH --> AUTHN
    FORWARD --> SPEND
    FORWARD --> RESP
    FORWARD --> TOKENS
    TOKENS -.-> AUDIT
```

### 6.2 配置数据流

```mermaid
graph TB
    subgraph HOST["宿主机文件系统"]
        DCYML["docker-compose.yml"]
        ENV[".env"]
        LCYML["config/litellm.yaml"]
        LPATCH["config/litellm-patch/*"]
        MCYML["config/monitoring/*"]
        HDIR["runtime/higress/*"]
    end

    subgraph BUILD["构建时"]
        DOCKERFILE["Dockerfile"]
        PATCHRUN["patch_streaming.py<br/>修改 streaming_iterator.py"]
        TESTGATE["test_empty_choices.py<br/>test_gateway_responses_compat.py"]
    end

    subgraph CONTAINERS["运行中容器"]
        LIMG["LiteLLM 镜像<br/>（已打补丁的 streaming_iterator.py<br/>gateway_responses_compat.py 位于 /app/）"]
        LHIG["Higress 容器<br/>（/data 卷 → xDS 控制器）"]
        LPROM["Prometheus 容器<br/>（采集配置位于 /etc/prometheus/）"]
        LGRAF["Grafana 容器<br/>（预配置位于 /etc/grafana/provisioning/）"]
    end

    DCYML -->|"env_file"| ENV
    DCYML -->|"volume mount（只读）"| LCYML
    DCYML -->|"volume mount（读写）"| HDIR
    DCYML -->|"volume mount（只读）"| MCYML
    LPATCH --> DOCKERFILE
    DOCKERFILE --> PATCHRUN
    DOCKERFILE --> TESTGATE
    DOCKERFILE --> LIMG
    LCYML -->|"挂载至 /app/config.yaml"| LIMG
    ENV -->|"加载为环境变量"| LIMG
    HDIR -->|"挂载至 /data"| LHIG
    MCYML -->|"挂载至 /etc/prometheus/"| LPROM
    MCYML -->|"挂载至 /etc/grafana/provisioning/"| LGRAF
```

### 6.3 监控数据流

```mermaid
graph LR
    subgraph METRICS["指标来源"]
        ENVOY["Envoy 数据面<br/>127.0.0.1:15000/stats/prometheus"]
        AGENT["Higress Agent<br/>127.0.0.1:15020/stats/prometheus"]
    end

    subgraph COLLECT["采集"]
        PROM["Prometheus<br/>15 秒采集间隔<br/>7 天保留"]
    end

    subgraph VISUALIZE["可视化"]
        GRAF["Grafana<br/>12 面板<br/>10 秒刷新"]
    end

    ENVOY -->|"每 15 秒采集"| PROM
    AGENT -->|"每 15 秒采集"| PROM
    PROM -->|"HTTP :19090<br/>Docker DNS"| GRAF
```

**注意：** 当前监控仅覆盖 Higress/Envoy 的网络层指标。LiteLLM 未配置 Prometheus 导出器，PostgreSQL 未部署 `postgres_exporter`。缺乏每密钥成本、每模型 TTFB（首 Token 延迟）和内容层可观测性。

---

## 7. 部署与启动顺序

### 7.1 主栈（`docker-compose.yml`）

```
阶段 1：db（PostgreSQL）
  └─ 创建/挂载外部卷 litellm_postgres_data
  └─ 启动 postgres:16 容器
  └─ 健康检查: pg_isready -U llmproxy -d litellm（5 秒间隔，最多 10 次重试）
  └─ 容器健康 → 继续

阶段 2：litellm（LiteLLM 代理）
  └─ 等待 db 服务健康（service_healthy）
  └─ 启动自定义镜像 api-gateway-litellm:responses-empty-choices-v1
  └─ 挂载 config/litellm.yaml 为 /app/config.yaml（只读）
  └─ 加载 .env 获取提供商 API 密钥（可选）
  └─ 运行: --config /app/config.yaml --port 4000
  └─ 连接 PostgreSQL 于 db:5432
  └─ 注册配置中的 8 个模型别名

阶段 3：higress（Higress 网关）
  └─ 等待 litellm 容器启动（无健康检查条件）
  └─ 启动 higress/all-in-one:latest
  └─ 挂载 runtime/higress/ 为 /data
  └─ 控制器读取 Ingress、ConfigMap、WasmPlugin、McpBridge YAML
  └─ 翻译为 Envoy xDS 配置
  └─ Envoy 数据面启动监听 :8080（HTTP）、:8443（HTTPS）、:8001（控制台）
```

### 7.2 监控栈（`docker-compose.monitoring.yml`）

```
阶段 4：prometheus
  └─ 使用 network_mode: container:api-gateway-higress
  └─ 共享 Higress 网络命名空间（无独立 IP）
  └─ 采集 127.0.0.1:15000（Envoy）和 127.0.0.1:15020（Agent）
  └─ 监听 0.0.0.0:19090

阶段 5：grafana
  └─ 加入 api-gateway-network，IP 为 172.30.50.5
  └─ 自动配置 Prometheus 数据源（http://api-gateway-higress:19090）
  └─ 自动配置 Higress 仪表板（12 面板）
  └─ 绑定至 127.0.0.1:3000（仅本地访问）
```

### 7.3 压测栈（`docker-compose.benchmark.yml`）

```
阶段 B1：init
  └─ 复制压测 Higress 配置到 runtime/gateway-benchmark/higress/
  └─ 创建 /reports 目录
  └─ 退出（一次性任务）

阶段 B2：mock
  └─ 启动 Node.js Mock 服务器于 :9000
  └─ 健康检查: GET /health（5 秒间隔，最多 12 次重试）

阶段 B3：litellm
  └─ 等待 mock 健康
  └─ 启动，配置 benchmark-chat 模型指向 mock:9000

阶段 B4：higress
  └─ 等待 init 完成、litellm 健康
  └─ 启动，加载压测 Ingress 规则

阶段 B5：k6（手动，通过 profiles: [tools] 启动）
  └─ 执行 gateway.k6.js 对 TARGET（mock/litellm/higress/full）压测
  └─ 报告写入 runtime/gateway-benchmark/reports/
```

---

## 8. Higress 与 LiteLLM 职责对比

| 能力 | Higress | LiteLLM |
|---|---|---|
| **层级** | 边缘入口（L4-L7 网关） | 应用代理（L7 AI 路由器） |
| **TLS 终止** | 支持（自动 HTTPS，ACME/Let's Encrypt） | 不支持 |
| **域名/虚拟主机路由** | 支持（Host 头匹配、ConfigMap 域名） | 不支持 |
| **传输层认证** | key-auth WASM 插件（Bearer Token、消费者级） | 不支持（依赖应用层密钥） |
| **速率限制** | 可通过 WASM 插件配置（当前未配置） | 每密钥/团队 RPM/TPM（当前未配置） |
| **IP/来源过滤** | 可通过 WAF/WASM 插件 | 不支持 |
| **熔断** | Envoy 级连接限制、上游健康检查 | 不支持 |
| **重试/故障转移** | proxy-next-upstream（路径 B：3 次尝试、120 秒） | 跨提供商回退（model_list 中配置） |
| **连接池** | 支持（HTTP/2、keep-alive、maxConcurrentStreams: 100） | 不支持（每请求创建新上游连接） |
| **模型抽象** | 不支持 | 8 个模型别名，跨 4 个提供商 |
| **协议适配** | 不支持（透明 HTTP 转发） | 跨 DashScope、DeepSeek、Agnes、自定义端点的 OpenAI 兼容标准化 |
| **业务层认证** | 不支持 | 每用户/团队虚拟密钥、模型访问权限、预算强制执行 |
| **成本记账** | 不支持 | Token 计数、消费日志条目、预算追踪 |
| **流式兼容** | 不支持 | 自定义 Patch 处理空 choices、SSE 标准化 |
| **功能翻译** | 不支持 | 嵌入（Embeddings）、图像生成、视觉输入、/responses API |
| **可观测性** | Envoy 指标（QPS、延迟、错误、上游健康） | Token 用量、消费数据、请求日志（PostgreSQL） |

### 分层原则

> **Higress 是守门员** —— 决定谁可以进入，执行基础设施级策略。
> **LiteLLM 是翻译官** —— 决定哪个模型处理请求，管理应用层关注点。
>
> Higress 看不到模型 Token 或成本数据，LiteLLM 不处理 TLS 或 IP 过滤。这种分离避免了两层同时执行同一 Token 速率限制的关键风险 —— 否则在重试和回退时会发生双重计数。

---

## 9. 具体请求示例

### 9.1 路径 A — 标准入口（无 Higress 认证）

#### 步骤 1：客户端 → Higress

```
POST http://localhost:8080/v1/chat/completions
Host: localhost
Authorization: Bearer sk-xxx-team-key-001
Content-Type: application/json
Accept: application/json

{
  "model": "my-qwen3.6-27b",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is the capital of France?"}
  ],
  "temperature": 0.7,
  "max_tokens": 256
}
```

#### 步骤 2：Higress → LiteLLM（不变 — 认证头透明转发）

```
POST http://172.30.50.2:4000/v1/chat/completions
Authorization: Bearer sk-xxx-team-key-001
Content-Type: application/json

{
  "model": "my-qwen3.6-27b",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is the capital of France?"}
  ],
  "temperature": 0.7,
  "max_tokens": 256
}
```

#### 步骤 3：LiteLLM → 上游提供商

```
POST https://test2-aigc.campusapp.com.cn/api/v1/chat/completions
Authorization: Bearer <ZHILIN_aigc_API_KEY>
Content-Type: application/json

{
  "model": "Qwen3.6-27B",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is the capital of France?"}
  ],
  "temperature": 0.7,
  "max_tokens": 256
}
```

#### 步骤 4：响应（非流式）

```
HTTP/1.1 200 OK
Server: envoy
Content-Type: application/json

{
  "id": "chatcmpl-xxxxx",
  "object": "chat.completion",
  "model": "my-qwen3.6-27b",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "The capital of France is Paris."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 28,
    "completion_tokens": 9,
    "total_tokens": 37
  }
}
```

### 9.2 路径 B — AI 路由入口（Higress 消费者认证）

#### 步骤 1：客户端 → Higress

```
POST http://localhost:8080/v1/chat/completions
Host: ai-test.local
Authorization: Bearer 3f8f9e70-8a3f-4470-825c-f1f783f72737
Content-Type: application/json; charset=utf-8

{
  "model": "my-kimi-k2.7-code",
  "messages": [
    {"role": "user", "content": "Write a Python function to sort a list."}
  ],
  "temperature": 0,
  "max_tokens": 512
}
```

#### 步骤 2：Higress → LiteLLM（ai-proxy WASM 重写认证）

```
POST http://172.30.50.2:4000/v1/chat/completions
Authorization: Bearer sk-local-test
Content-Type: application/json; charset=utf-8

{
  "model": "my-kimi-k2.7-code",
  "messages": [
    {"role": "user", "content": "Write a Python function to sort a list."}
  ],
  "temperature": 0,
  "max_tokens": 512
}
```

#### 步骤 3：LiteLLM → 上游提供商（DashScope）

```
POST https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions
Authorization: Bearer <DASHSCOPE_API_KEY>
Content-Type: application/json

{
  "model": "kimi-k2.7-code",
  "messages": [
    {"role": "user", "content": "Write a Python function to sort a list."}
  ],
  "temperature": 0,
  "max_tokens": 512
}
```

### 9.3 流式请求（SSE）

#### 步骤 1：客户端 → Higress（流式）

```
POST http://localhost:8080/v1/chat/completions
Authorization: Bearer sk-xxx-team-key-001
Accept: text/event-stream

{
  "model": "my-deepseek-v4-flash",
  "messages": [{"role": "user", "content": "Explain quantum computing."}],
  "stream": true,
  "stream_options": {"include_usage": true}
}
```

#### 步骤 2：响应（SSE 事件）

```
HTTP/1.1 200 OK
Server: envoy
Content-Type: text/event-stream

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Quantum"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":" computing"},"finish_reason":null}]}

...

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

### 9.4 流式故障场景（空 Choices — Patch 修复）

若没有流式 Patch，如下空 choices 数据块会导致 `IndexError`：

```
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[]}
```

在 `streaming_iterator.py` 中打补丁后的 `_ensure_output_item_for_chunk` 检测到 `len(chunk.choices) == 0` 时跳过该数据块，而不是访问 `chunk.choices[0]`。

---

## 10. 网络拓扑

### 10.1 主网络（`api-gateway-network`）

| 子网 | 网关 | 用途 |
|---|---|---|
| `172.30.50.0/24` | — | 主生产网络 |

| 服务 | 静态 IP | 宿主机端口 | 内部端口 |
|---|---|---|---|
| litellm | 172.30.50.2 | 4000 | 4000 |
| higress | 172.30.50.3 | 8080, 8443, 8001 | 8080, 8443, 8001, 15000, 15020, 15051 |
| db | 172.30.50.4 | 无 | 5432 |
| grafana | 172.30.50.5 | 127.0.0.1:3000 | 3000 |
| prometheus | （与 higress 共享命名空间） | 无 | 19090 |

### 10.2 压测网络（`benchmark`）

| 子网 | 用途 |
|---|---|
| `172.30.51.0/24` | 隔离压测网络 |

| 服务 | 静态 IP | 宿主机端口 | 内部端口 |
|---|---|---|---|
| litellm | 172.30.51.2 | 127.0.0.1:14000 | 4000 |
| higress | 172.30.51.3 | 127.0.0.1:18080, 127.0.0.1:18001 | 8080, 8001 |
| mock | 172.30.51.4 | 127.0.0.1:19000 | 9000 |

### 10.3 宿主机端口汇总

| 端口 | 服务 | 绑定地址 | 可访问来源 |
|---|---|---|---|
| 8080 | Higress HTTP | 0.0.0.0 | 任意网卡（安全关注点） |
| 8443 | Higress HTTPS | 0.0.0.0 | 任意网卡（安全关注点） |
| 8001 | Higress 控制台 | 0.0.0.0 | 任意网卡（安全关注点） |
| 4000 | LiteLLM | 0.0.0.0 | 任意网卡（安全关注点） |
| 3000 | Grafana | 127.0.0.1 | 仅本机 |

---

## 11. 安全模型

### 11.1 认证层次

| 层级 | 机制 | 范围 | 当前状态 |
|---|---|---|---|
| **Higress 边缘（路径 B）** | key-auth WASM 插件，Bearer Token | 仅 `ai-test.local` 虚拟主机 | 单一消费者 `test-client`，硬编码 Token |
| **Higress 边缘（路径 A）** | 无（key-auth 已禁用） | 默认域名 | 网关层无认证 |
| **LiteLLM 应用层** | PostgreSQL 虚拟密钥、主密钥环境变量 | 所有模型、所有路径 | 主密钥 `sk-local-test` 硬编码；数据库中有 6 个验证 Token（4 个已过期） |
| **上游提供商** | 提供商专有 API 密钥 | 每提供商调用 | 密钥存储在 `.env`（未提交至 Git） |

### 11.2 关键安全关注点

1. **端口广泛绑定**：端口 8080、8443、8001 和 4000 均绑定 `0.0.0.0`，从任意网卡均可访问。
2. **路径 A 无网关认证**：标准入口完全绕过 Higress key-auth，安全性依赖 LiteLLM 虚拟密钥。
3. **路径 B 所有请求使用主密钥**：ai-proxy WASM 将所有已认证消费者的密钥重写为 LiteLLM 主密钥 `sk-local-test`，丢失每消费者身份，无法进行成本归因。
4. **key-auth 设为 FAIL_OPEN**：key-auth 插件未启用 fail-safe 行为。
5. **硬编码凭据**：`LITELLM_MASTER_KEY=sk-local-test` 和数据库密码 `dbpassword9090` 硬编码在 `docker-compose.yml` 中。
6. **至 LiteLLM 无 TLS**：Higress 与 LiteLLM 之间的内部流量为未加密的 HTTP。

---

## 12. 已知缺口与风险

### 12.1 架构缺口

| 缺口 | 影响 | 优先级 |
|---|---|---|
| 未接入校园 SSO / SCIM | 无法认证真实校园用户 | P0 |
| LiteLLM 层无每消费者身份 | 路径 B 所有请求使用主密钥，无法归因成本或执行每用户配额 | P0 |
| 未配置速率限制（Higress 和 LiteLLM 均无） | 无防滥用或提供商配额耗尽保护 | P0 |
| 无业务层故障转移或会话保持 | 单提供商故障导致完全中断 | P1 |
| 监控仅覆盖网关层 | 无 LiteLLM 成本指标、无每密钥统计、无模型 TTFB | P1 |
| PostgreSQL 消费数据所有 236 条记录 spend=0 | 无法进行成本对账 | P0 |
| 内容审计 / 安全过滤器未接入 | 无有害内容防护栏 | P1 |
| 无模型市场 / 自助服务门户 | 用户无法发现或申请模型访问 | P1 |
| 无订阅 / OAuth 集成 | 外部提供商订阅未管理 | P1 |

### 12.2 运维风险

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| 压测容器退出码 137 | 可能 OOM，容量数据可能不可靠 | 增加 Docker 内存限制，重新执行正式压测 |
| 运行版 LiteLLM 1.100.0 与源码 1.102.0 不一致 | 功能/Bug 差异 | 对齐版本，说明分歧原因 |
| Git 仓库根分支零提交 | 项目无法从克隆复现 | 创建初始提交，添加 `.gitmodules` 管理 sources |
| 两项已确认的测试误报 | 验证结果可能不可靠 | 修复 `test_dual_gateway.py`（接受空 `{}` 为成功）和 `mock.test.mjs`（逗号表达式 Bug） |
| Higress WASM key-auth 设为 FAIL_OPEN | 安全策略未强制执行 | 生产环境设为 FAIL_CLOSE |

### 12.3 配置风险

| 风险 | 详情 |
|---|---|
| `.env.example` 不完整 | 仅列出 `MODEL_API_KEY`；实际配置需要 `DASHSCOPE_API_KEY`、`ZHILIN_aigc_API_KEY`、`DEEPSEEK_API_KEY`、`AGNES_API_KEY` |
| 密钥和团队无 RPM/TPM 限制 | 236 条消费日志存在但无速率限制保护 |
| 路径 B 重试策略包含非幂等 | POST 请求在非幂等标记下重试可能导致重复模型调用 |
| Prometheus 仅保留 7 天 | 不足以进行趋势分析或月末成本报告 |

---

## 附录 A：文件索引

### 核心配置文件

| 文件 | 用途 |
|---|---|
| `docker-compose.yml` | 主栈：db、litellm、higress |
| `docker-compose.benchmark.yml` | 压测栈：init、mock、litellm、higress、k6 |
| `docker-compose.monitoring.yml` | 监控栈：prometheus、grafana |
| `.env` | 提供商 API 密钥（已排除 Git） |
| `.env.example` | 模板（不完整） |
| `config/litellm.yaml` | LiteLLM 模型列表、主密钥、设置、回调 |

### LiteLLM Patch 文件

| 文件 | 用途 |
|---|---|
| `config/litellm-patch/Dockerfile` | 自定义 LiteLLM 镜像构建 |
| `config/litellm-patch/gateway_responses_compat.py` | Qwen Responses 回调（构建时打入镜像） |
| `config/litellm-patch/patch_streaming.py` | 流式迭代器 Patch 脚本（构建时运行） |
| `config/litellm-patch/test_empty_choices.py` | 流式 Patch 单元测试 |
| `config/litellm-patch/test_gateway_responses_compat.py` | 回调单元测试 |
| `config/litellm-patch/compose.rollback.yml` | 回滚 Compose 覆盖 |

### 监控配置

| 文件 | 用途 |
|---|---|
| `config/monitoring/prometheus.yml` | Prometheus 采集配置 |
| `config/monitoring/provisioning/datasources/prometheus.yml` | Grafana 数据源预配置 |
| `config/monitoring/provisioning/dashboards/higress.yml` | Grafana 仪表板提供者 |
| `config/monitoring/dashboards/higress.json` | 12 面板 Higress 仪表板定义 |

### Higress 运行时配置（挂载至 `/data`）

| 文件 | 用途 |
|---|---|
| `runtime/higress/configmaps/higress-config.yaml` | 全局网关配置 |
| `runtime/higress/configmaps/higress-https.yaml` | TLS/ACME 设置 |
| `runtime/higress/configmaps/domain-higress-default-domain.yaml` | 默认域名 |
| `runtime/higress/configmaps/domain-ai-test.local.yaml` | ai-test.local 域名 |
| `runtime/higress/configmaps/ai-route-litellm-ai-test.yaml` | AI 路由 JSON 配置 |
| `runtime/higress/configmaps/higress-console.yaml` | 控制台设置 |
| `runtime/higress/ingresses/default.yaml` | 控制台入口（/） |
| `runtime/higress/ingresses/litellm-proxy.yaml` | 标准 /v1 入口 |
| `runtime/higress/ingresses/ai-route-litellm-ai-test.internal.yaml` | AI 路由 /v1 入口 |
| `runtime/higress/mcpbridges/default.yaml` | 静态服务注册 |
| `runtime/higress/wasmplugins/key-auth.internal.yaml` | 密钥认证 WASM 插件 |
| `runtime/higress/wasmplugins/ai-proxy.internal.yaml` | AI 代理 WASM 插件 |
| `runtime/higress/wasmplugins/ai-statistics-2.0.1.yaml` | AI 统计 WASM 插件 |
| `runtime/higress/envoyfilters/higress-http-resolver-cluster.yaml` | HTTP 解析器集群 |
| `runtime/higress/envoyfilters/higress-gateway-global-custom-response.yaml` | 自定义响应 |

### 测试文件

| 文件 | 用途 |
|---|---|
| `test/test_gateway_models.py` | 模型端点验证（在线） |
| `test/test_gateway_resilience.py` | 错误处理与并发负载（在线） |
| `test/test_gateway_resilience_unit.py` | 弹性单元测试（离线） |
| `test/test_dual_gateway.py` | 双入口对比与认证（在线） |
| `test/test_dual_gateway_unit.py` | 双入口认证单元测试（离线） |
| `test/test_higress_route.py` | 单 AI 路由 + Host 头（在线） |
| `test/test_responses_patch_live.py` | /responses API 冒烟测试（在线） |
| `test/benchmark/gateway.k6.js` | k6 压测脚本 |
| `test/benchmark/mock.mjs` | Mock API 服务器 |
| `test/benchmark/mock.test.mjs` | Mock 服务器单元测试 |

### 文档

| 文件 | 用途 |
|---|---|
| `doc/校园统一AI网关产品需求文档.md` | 产品需求文档（PRD V1.0） |
| `doc/校园统一AI网关时间节点计划.md` | 项目时间节点计划 |
| `doc/项目现状及后续实施计划-2026-09-16.md` | 当前状态与实施计划 |
| `doc/项目现状核查-验证记录-2026-09-16.md` | 验证记录 |
| `doc/higress-litellm-evaluation.md` | Higress + LiteLLM 方案评估 |
| `doc/gateway-capacity-benchmark.md` | 容量压测结果 |
| `doc/gateway-resilience-validation.md` | 弹性验证结果 |
| `doc/model-capability-validation.md` | 模型能力验证指南 |
| `doc/higress-monitoring-local.md` | 本地监控部署指南 |
| `doc/architecture.md` | 架构文档（本文档英文版） |
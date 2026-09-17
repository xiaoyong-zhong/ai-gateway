# Campus Unified AI Gateway — Architecture Document

**Version:** 1.0
**Date:** 2026-09-16
**Repository:** `d:\zhilin\API网关` (branch: `test`)

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture Diagram](#2-architecture-diagram)
3. [Component Responsibility Table](#3-component-responsibility-table)
4. [Request Flow — Chat Completion API](#4-request-flow--chat-completion-api)
5. [Configuration Chain](#5-configuration-chain)
6. [Data Flow Analysis](#6-data-flow-analysis)
7. [Deployment and Startup Sequence](#7-deployment-and-startup-sequence)
8. [Higress vs LiteLLM — Role Comparison](#8-higress-vs-litellm--role-comparison)
9. [Concrete Request Examples](#9-concrete-request-examples)
10. [Known Issues and Open Questions](#10-known-issues-and-open-questions)

---

## 1. System Overview

The Campus Unified AI Gateway is a layered API gateway platform that provides a single, OpenAI-compatible entry point for AI model services across a university campus. It combines two complementary systems:

- **Higress** (Apache Incubating) — An Envoy-based API gateway serving as the edge ingress layer. It handles domain routing, TLS termination, key authentication, traffic governance, WAF, rate limiting, and infrastructure-level observability.
- **LiteLLM** — A Python-based LLM proxy serving as the model-routing layer. It provides protocol adaptation across multiple providers, model aliases, virtual key management, token/cost accounting, retry/fallback, and business-level authorization.

Three additional infrastructure services complete the stack:

- **PostgreSQL 16** — Persistent backend for LiteLLM's proxy state (users, teams, keys, SpendLogs, budgets).
- **Prometheus** — Scrapes Envoy/Higress metrics at 15-second intervals.
- **Grafana** — Visualizes gateway metrics via an auto-provisioned 12-panel dashboard.

The system currently supports **8 configured model aliases** across 4 upstream providers: Alibaba DashScope, Zhilin Campus AIGC, DeepSeek, and Agnes-AI.

---

## 2. Architecture Diagram

```mermaid
graph TB
    subgraph Client["Clients"]
        C1["Application / Developer"]
        C2["Campus SSO User"]
    end

    subgraph Edge["Edge Layer — Higress (Envoy)"]
        direction TB
        H_INGRESS["HTTP Ingress<br/>0.0.0.0:8080"]
        H_HTTPS["HTTPS Ingress<br/>0.0.0.0:8443"]
        H_CONSOLE["Console<br/>0.0.0.0:8001"]

        subgraph WASM["WASM Plugin Chain"]
            H_AUTHN["key-auth<br/>(priority 310 / AUTHN)"]
            H_AIPROXY["ai-proxy<br/>(priority 100 / AI)"]
            H_STATS["ai-statistics<br/>(priority 900 / STATS)"]
        end

        H_ROUTE["Ingress Router<br/>(Host + Path match)"]
        H_MCPSVC["McpBridge Registry<br/>(static service discovery)"]
        H_METRICS["Envoy Metrics<br/>127.0.0.1:15000"]
        H_AGENT_METRICS["Agent Metrics<br/>127.0.0.1:15020"]
    end

    subgraph Model["Model Layer — LiteLLM (Python)"]
        L_ENTRY["LiteLLM Proxy<br/>0.0.0.0:4000"]
        L_KEYAUTH["Virtual Key Auth<br/>(PostgreSQL lookup)"]
        L_ROUTER["Model Router<br/>(config.yaml aliases)"]
        L_CALLBACK["gateway_responses_compat<br/>(Qwen instruction merge)"]
        L_PATCH["streaming_iterator patch<br/>(empty-choices guard)"]
        L_ACCOUNT["Token/Cost Accounting<br/>(SpendLog writes)"]
    end

    subgraph Data["Data Layer"]
        PG["PostgreSQL 16<br/>172.30.50.4:5432"]
        PG_DATA["litellm_postgres_data<br/>(external named volume)"]
    end

    subgraph Monitor["Observability"]
        P["Prometheus<br/>scrape 15s, retain 7d"]
        G["Grafana<br/>127.0.0.1:3000"]
        G_DASH["12-panel Dashboard<br/>(QPS, errors, latency, upstream)"]
    end

    subgraph Providers["Upstream Model Providers"]
        DASH["DashScope (Alibaba)<br/>dashscope.aliyuncs.com"]
        ZHILIN["Zhilin Campus AIGC<br/>test2-aigc.campusapp.com.cn"]
        DS["DeepSeek<br/>api.deepseek.com"]
        AG["Agnes AI<br/>apihub.agnes-ai.com"]
    end

    C1 --> H_INGRESS
    C2 --> H_INGRESS
    C1 --> H_HTTPS

    H_INGRESS --> H_ROUTE
    H_HTTPS --> H_ROUTE

    H_ROUTE --> H_AUTHN
    H_AUTHN --> H_AIPROXY
    H_AIPROXY --> H_MCPSVC
    H_MCPSVC -->|"litellm.static / llm-litellm.internal.static<br/>→ 172.30.50.2:4000"| L_ENTRY

    L_ENTRY --> L_KEYAUTH
    L_KEYAUTH --> L_ROUTER
    L_ROUTER --> L_CALLBACK
    L_CALLBACK --> L_PATCH
    L_PATCH --> L_ACCOUNT
    L_ACCOUNT -->|"records SpendLogs"| PG

    L_ROUTER -->|"model alias → provider"| DASH
    L_ROUTER --> ZHILIN
    L_ROUTER --> DS
    L_ROUTER --> AG

    PG --> PG_DATA

    H_METRICS -->|"shared network namespace"| P
    H_AGENT_METRICS --> P
    P -->|"api-gateway-higress:19090"| G
    G --> G_DASH

    H_STATS -.->|"token stats → ai_log<br/>access log"| H_METRICS
```

### Network Topology

| Service | Container Name | Network | Static IP | Host Ports |
|---|---|---|---|---|
| LiteLLM | `api-gateway-litellm` | `api-gateway-network` | `172.30.50.2` | `4000` |
| Higress | `api-gateway-higress` | `api-gateway-network` | `172.30.50.3` | `8001`, `8080`, `8443` |
| PostgreSQL | `litellm_db` | `api-gateway-network` | `172.30.50.4` | (none — internal only) |
| Prometheus | `api-gateway-monitoring-prometheus` | (shared NS with Higress) | (none) | (none — internal, `:19090`) |
| Grafana | `api-gateway-grafana` | `api-gateway-network` | `172.30.50.5` | `127.0.0.1:3000` |

**Network subnets:**
- `api-gateway-network`: `172.30.50.0/24`
- `benchmark` (load testing): `172.30.51.0/24`

---

## 3. Component Responsibility Table

| Component | Layer | Primary Responsibility | Key Capabilities | Config Source |
|---|---|---|---|---|
| **Higress** | Edge ingress | Gateway traffic governance and routing | Domain routing, TLS termination, key auth (WASM), rate limiting, WAF, IP filtering, connection pooling, HTTP/2 multiplexing, failover/retry, Envoy metrics export | `runtime/higress/` YAML directory (Ingress, ConfigMap, WasmPlugin, McpBridge, EnvoyFilter resources watched by embedded controller → xDS push) |
| **key-auth (WASM)** | Higress plugin | Consumer identity verification at gateway edge | Bearer token validation, consumer allowlists, FAIL_OPEN/FAIL_CLOSE mode, per-ingress enable/disable | `runtime/higress/wasmplugins/key-auth.internal.yaml` |
| **ai-proxy (WASM)** | Higress plugin | AI-specific request transformation | Authorization header rewrite (injects LiteLLM service key), destination rewrite to LiteLLM endpoint, OpenAI protocol passthrough | `runtime/higress/wasmplugins/ai-proxy.internal.yaml` |
| **ai-statistics (WASM)** | Higress plugin | Token usage collection at gateway level | Parses response for token counts, injects `ai_log` into Envoy access logs for Prometheus scraping | `runtime/higress/wasmplugins/ai-statistics-2.0.1.yaml` |
| **McpBridge** | Higress service registry | Static service discovery | Maps logical names (`litellm.static`, `llm-litellm.internal.static`) to IP:port (`172.30.50.2:4000`) | `runtime/higress/mcpbridges/default.yaml` |
| **LiteLLM** | Model routing | Unified LLM proxy and protocol adaptation | OpenAI-compatible API (`/v1/chat/completions`, `/v1/embeddings`, `/v1/images/generations`, `/v1/responses`), model alias resolution, multi-provider routing, virtual key auth, budget enforcement, retry/fallback, streaming normalization | `config/litellm.yaml` (mounted as `/app/config.yaml`), `.env` (provider API keys) |
| **gateway_responses_compat (callback)** | LiteLLM patch | Qwen-specific request compatibility | Merges leading `system`/`developer` messages into `instructions` field for `/v1/responses` calls on `my-qwen3.6-27b`, preventing chat template rejection | `config/litellm-patch/gateway_responses_compat.py` (COPYed into image at `/app/` during build) |
| **streaming_iterator patch** | LiteLLM patch | Empty-choice crash prevention | 3 guards in `streaming_iterator.py`: skip empty `choices` arrays in `_ensure_output_item_for_chunk`, `_is_reasoning_end`, and text-delta extraction | `config/litellm-patch/patch_streaming.py` (applied during `docker build`) |
| **PostgreSQL 16** | Data persistence | LiteLLM proxy state backend | Stores users, teams, API keys (virtual keys), SpendLogs, token budgets, audit records, SSO config, organizations | Docker volume `litellm_postgres_data`; credentials via compose env vars (`POSTGRES_DB=litellm`, `POSTGRES_USER=llmproxy`) |
| **Prometheus** | Observability | Metrics collection and storage | Scrapes Envoy data plane (`:15000`) and Higress agent (`:15020`) every 15s; retains 7 days | `config/monitoring/prometheus.yml`; uses `network_mode: container:api-gateway-higress` |
| **Grafana** | Observability | Metrics visualization | Auto-provisioned 12-panel dashboard: scrape health, uptime, QPS, HTTP status codes, 5xx ratio, upstream rate/timeouts/retries/latency, listener health, active requests | `config/monitoring/provisioning/` (datasources, dashboards), `config/monitoring/dashboards/higress.json` |

---

## 4. Request Flow — Chat Completion API

### 4.1 Ingress Paths

The system supports **two distinct ingress paths** into Higress, each with a different authentication model. Both converge at the same LiteLLM backend.

| Aspect | Path A — Standard Ingress | Path B — AI Route (dedicated host) |
|---|---|---|
| **URL** | `POST http://localhost:8080/v1/chat/completions` | `POST http://localhost:8080/v1/chat/completions` |
| **Host header** | Default (e.g., `localhost`) | `ai-test.local` |
| **Authorization** | `Bearer <LiteLLM Virtual Key>` (e.g., `sk-xxx`) | `Bearer <Higress Consumer Key>` (e.g., `3f8f9e70-8a3f-4470-825c-f1f783f72737`) |
| **Higress key-auth** | Disabled (`configDisable: true` on `default` ingress) | Enabled (`configDisable: false` on `ai-route-litellm-ai-test.internal` ingress) |
| **Higress ai-proxy** | Not applied (standard Ingress forwarding) | Applied — rewrites `Authorization` to `Bearer sk-local-test` (LiteLLM master key) |
| **Higress ai-statistics** | Not applied | Applied — collects token stats in `ai_log` |
| **Retry policy** | Not configured | 3 attempts, 120s timeout, on error/timeout/non-idempotent |
| **LiteLLM key validated** | Client's virtual key (DB lookup) | Master key `sk-local-test` (no per-consumer accounting at LiteLLM) |

> **Critical design note:** Path B uses the LiteLLM master key for all requests. The original Higress consumer identity is not forwarded to LiteLLM, meaning per-consumer token accounting, budget enforcement, and model permissions at the LiteLLM level do not apply. This is identified as P0 issue #1 in the project status assessment.

### 4.2 Sequence Diagram — Path A (Standard Ingress)

```mermaid
sequenceDiagram
    participant Client
    participant Higress as Higress (Envoy)
    participant KeyAuth as key-auth WASM
    participant LiteLLM as LiteLLM Proxy
    participant Callback as gateway_responses_compat
    participant Patch as streaming_iterator patch
    participant PG as PostgreSQL
    participant Provider as Upstream Provider

    Client->>Higress: POST /v1/chat/completions
    Note over Client,Higress: Host: localhost<br/>Authorization: Bearer sk-xxx

    Higress->>KeyAuth: AUTHN phase (priority 310)
    KeyAuth-->>Higress: configDisable=true → pass through

    Higress->>Higress: Ingress match: default /v1 Prefix<br/>→ destination litellm.static:80

    Higress->>LiteLLM: POST /v1/chat/completions
    Note over Higress,LiteLLM: Authorization: Bearer sk-xxx (unchanged)

    LiteLLM->>PG: Validate virtual key sk-xxx
    PG-->>LiteLLM: Key active, model permitted, budget OK

    LiteLLM->>LiteLLM: Model lookup: my-qwen3.6-27b<br/>→ openai/Qwen3.6-27B<br/>→ test2-aigc.campusapp.com.cn

    alt call_type == "aresponses"
        LiteLLM->>Callback: Pre-call hook
        Callback->>Callback: Merge leading system/developer<br/>messages into instructions
        Callback-->>LiteLLM: Modified request
    else call_type != "aresponses" (standard chat)
        Note over LiteLLM,Callback: Callback is no-op for /v1/chat/completions
    end

    LiteLLM->>Provider: POST /api/v1/chat/completions
    Note over LiteLLM,Provider: Authorization: Bearer <ZHILIN_aigc_API_KEY>

    Provider-->>LiteLLM: JSON or SSE stream response

    alt streaming response
        LiteLLM->>Patch: streaming_iterator processes chunks
        Patch->>Patch: Guard 1: skip empty choices<br/>Guard 2: reasoning end guard<br/>Guard 3: text-delta empty guard
        Patch-->>LiteLLM: Clean SSE events
    end

    LiteLLM->>PG: INSERT SpendLog (token counts, cost)

    LiteLLM-->>Higress: JSON response or SSE stream

    Higress-->>Client: Response (Server: envoy header)
```

### 4.3 Sequence Diagram — Path B (AI Route with Consumer Auth)

```mermaid
sequenceDiagram
    participant Client
    participant Higress as Higress (Envoy)
    participant KeyAuth as key-auth WASM
    participant AIProxy as ai-proxy WASM
    participant AIStats as ai-statistics WASM
    participant LiteLLM as LiteLLM Proxy
    callback as gateway_responses_compat
    Patch as streaming_iterator patch
    PG as PostgreSQL
    Provider as Upstream Provider

    Client->>Higress: POST /v1/chat/completions
    Note over Client,Higress: Host: ai-test.local<br/>Authorization: Bearer 3f8f9e70-...

    Higress->>KeyAuth: AUTHN phase (priority 310)
    KeyAuth->>KeyAuth: Verify Bearer token<br/>against consumer test-client
    alt key valid
        KeyAuth-->>Higress: 200 OK
    else key invalid
        KeyAuth-->>Client: 401 Unauthorized<br/>(request never reaches LiteLLM)
    end

    Higress->>AIProxy: AI-PROXY phase (priority 100)
    AIProxy->>AIProxy: Rewrite Authorization<br/>→ Bearer sk-local-test
    AIProxy->>AIProxy: Rewrite destination<br/>→ http://172.30.50.2:4000/v1

    Higress->>LiteLLM: POST /v1/chat/completions
    Note over Higress,LiteLLM: Authorization: Bearer sk-local-test (master key)

    LiteLLM->>LiteLLM: Master key validated (config match)
    LiteLLM->>LiteLLM: Model lookup: my-qwen3.6-27b
    LiteLLM->>Provider: POST /api/v1/chat/completions

    Provider-->>LiteLLM: Response

    alt streaming
        LiteLLM->>Patch: streaming_iterator guards
        Patch-->>LiteLLM: Clean SSE events
    end

    LiteLLM->>PG: INSERT SpendLog (master key context)
    LiteLLM-->>Higress: Response

    Higress->>AIStats: STATS phase (priority 900)
    AIStats->>AIStats: Parse token counts → ai_log

    alt upstream failure
        Higress->>Higress: proxy-next-upstream retry<br/>(3 attempts, 120s, error/timeout/non-idempotent)
    end

    Higress-->>Client: Response (Server: envoy header)
```

### 4.4 Higress WASM Plugin Phase Chain

| Phase | Priority | Plugin | Path A | Path B |
|---|---|---|---|---|
| `AUTHN` | 310 | `key-auth` | Disabled | Validates consumer key |
| `AI` | 100 | `ai-proxy` | Not matched | Rewrites auth header + destination |
| `STATS` | 900 | `ai-statistics` | Not matched | Collects token stats |

---

## 5. Configuration Chain

### 5.1 Configuration Loading Overview

```mermaid
graph LR
    subgraph Host["Host Files"]
        DC["docker-compose.yml"]
        DCB["docker-compose.benchmark.yml"]
        DCM["docker-compose.monitoring.yml"]
        ENV[".env"]
        ENV_EX[".env.example"]
        LL_CONFIG["config/litellm.yaml"]
        LL_PATCH["config/litellm-patch/"]
        MON_CFG["config/monitoring/"]
        HIG_DATA["runtime/higress/"]
    end

    subgraph Containers["Containers"]
        DB["PostgreSQL"]
        L["LiteLLM"]
        H["Higress"]
        P["Prometheus"]
        G["Grafana"]
    end

    DC -->|"env vars, volumes, depends_on"| DB
    DC -->|"--config /app/config.yaml<br/>env vars, env_file"| L
    DC -->|"volumes: runtime/higress/:/data"| H
    DCM -->|"volumes: prometheus.yml"| P
    DCM -->|"volumes: provisioning, dashboards"| G

    ENV -->|"env_file"| L
    LL_CONFIG -->|"volume mount :ro"| L
    LL_PATCH -->|"COPY at build time"| L
    HIG_DATA -->|"volume mount /data"| H
    MON_CFG -->|"volume mounts :ro"| P
    MON_CFG -->|"volume mounts :ro"| G
```

### 5.2 Docker Compose Configuration

**File:** `d:\zhilin\API网关\docker-compose.yml`

| Mechanism | Target | Details |
|---|---|---|
| `environment` (inline) | `db` | `POSTGRES_DB=litellm`, `POSTGRES_USER=llmproxy`, `POSTGRES_PASSWORD=dbpassword9090` |
| `environment` (inline) | `litellm` | `LITELLM_MASTER_KEY=sk-local-test`, `DATABASE_URL=postgresql://llmproxy:dbpassword9090@db:5432/litellm` |
| `env_file` | `litellm` | `.env` (optional, `required: false`) — provider API keys (`DASHSCOPE_API_KEY`, `ZHILIN_aigc_API_KEY`, `DEEPSEEK_API_KEY`, `AGNES_API_KEY`) |
| `volumes` (bind, read-only) | `litellm` | `./config/litellm.yaml:/app/config.yaml:ro` |
| `volumes` (bind, read-write) | `higress` | `./runtime/higress:/data` — all Higress Kubernetes-style resources |
| `volumes` (named, external) | `db` | `litellm_postgres_data:/var/lib/postgresql/data` |
| `command` | `litellm` | `["--config", "/app/config.yaml", "--port", "4000"]` |
| `build` | `litellm` | `./config/litellm-patch/Dockerfile` — custom patched image |

### 5.3 LiteLLM Configuration

**File:** `d:\zhilin\API网关\config\litellm.yaml`

| Setting | Value | Effect |
|---|---|---|
| `model_list[].model_name` | 8 aliases (`my-kimi-k2.7-code`, `my-qwen3.5-ocr`, etc.) | Client-facing model names |
| `model_list[].litellm_params.model` | Upstream model IDs (`openai/kimi-k2.7-code`, etc.) | Resolved provider models |
| `model_list[].litellm_params.api_base` | Provider endpoints (DashScope, Zhilin, DeepSeek, Agnes) | Where requests are forwarded |
| `model_list[].litellm_params.api_key` | Environment variable references (`os.environ/DASHSCOPE_API_KEY`, etc.) | Auth to upstream providers |
| `general_settings.master_key` | `os.environ/LITELLM_MASTER_KEY` (`sk-local-test`) | Master API key for direct auth |
| `litellm_settings.drop_params` | `true` | Unknown params silently dropped |
| `litellm_settings.callbacks` | `[gateway_responses_compat.callback]` | Custom pre-call hook loaded from image |

**Model routing table:**

| Alias | Upstream Model | Provider Endpoint | API Key Source |
|---|---|---|---|
| `my-kimi-k2.7-code` | `openai/kimi-k2.7-code` | DashScope | `DASHSCOPE_API_KEY` |
| `my-qwen3.5-ocr` | `openai/qwen3.5-ocr` | DashScope | `DASHSCOPE_API_KEY` |
| `qwen3.7-flash` | `openai/qwen3.5-ocr` | DashScope | `DASHSCOPE_API_KEY` |
| `my-qwen3.7-text-embedding` | `openai/qwen3.7-text-embedding` | DashScope | `DASHSCOPE_API_KEY` |
| `my-qwen3.6-27b` | `openai/Qwen3.6-27B` | `test2-aigc.campusapp.com.cn` | `ZHILIN_aigc_API_KEY` |
| `my-deepseek-v4-flash` | `openai/deepseek-v4-flash` | DeepSeek | `DEEPSEEK_API_KEY` |
| `my-agnes-2.5-pro` | `openai/agnes-2.5-pro` | Agnes AI | `AGNES_API_KEY` |
| `my-agnes-image-2.5-flash` | `openai/agnes-image-2.5-flash` | Agnes AI | `AGNES_API_KEY` |

### 5.4 LiteLLM Patch Suite

**Directory:** `d:\zhilin\API网关\config\litellm-patch\`

| File | Build/Runtime | Purpose |
|---|---|---|
| `Dockerfile` | Build | Pinned base image (`sha256:c8756e...`); COPYs callback, runs patch + tests |
| `gateway_responses_compat.py` | Runtime (`/app/`) | `QwenResponsesCompatibility` callback class — merges leading system/developer messages for `/v1/responses` on `my-qwen3.6-27b` |
| `patch_streaming.py` | Build-time only | Applies 3 surgical guards to `streaming_iterator.py` (empty-choices `IndexError` fix); anchored to base image SHA-256 |
| `test_empty_choices.py` | Build gate | 6 offline tests for streaming patch (empty chunk handling, reasoning, tool calls) |
| `test_gateway_responses_compat.py` | Build gate | 7 offline tests for callback (instruction merge, passthrough, edge cases) |
| `compose.rollback.yml` | Rollback | Points to original unpatched base image; keeps callback via volume mount |
| `README.md` | Documentation | Scope, build/verify steps, rollback, known limits |

**Total build-time tests:** 13 (6 streaming + 7 callback). Tests must pass for the image to build.

### 5.5 Higress Configuration

**Directory:** `d:\zhilin\API网关\runtime\higress\`

Higress uses a Kubernetes-style control plane stored as YAML on disk. The embedded controller watches `/data` and translates resources into Envoy xDS configuration. No single config file — configuration is distributed across resource types.

| Resource Type | File(s) | Purpose |
|---|---|---|
| **ConfigMap (global)** | `configmaps/higress-config.yaml` | Mesh settings, downstream connection limits (`32768`), idle timeouts (`180s`), upstream limits (`10MB`/`10s`), access log format (JSON with `ai_log`) |
| **ConfigMap (TLS)** | `configmaps/higress-https.yaml` | Auto-HTTPS, ACME/Let's Encrypt (currently empty keys) |
| **ConfigMap (domains)** | `configmaps/domain-higress-default-domain.yaml` | Default domain, HTTPS enabled |
| **ConfigMap (domains)** | `configmaps/domain-ai-test.local.yaml` | `ai-test.local` domain, HTTPS off |
| **ConfigMap (AI route)** | `configmaps/ai-route-litellm-ai-test.yaml` | JSON AI route: domain, path predicate, upstream, auth, retry policy |
| **ConfigMap (console)** | `configmaps/higress-console.yaml` | Admin console settings |
| **ConfigMap (CRL)** | `configmaps/istio-ca-crl.yaml` | Certificate revocation list |
| **Ingress** | `ingresses/default.yaml` | Routes `/` (Exact) → Higress console (`127.0.0.1:8001`), path rewrite to `/landing` |
| **Ingress** | `ingresses/litellm-proxy.yaml` | Routes `/v1` (Prefix) → `litellm.static:80` (`172.30.50.2:4000`) on default domain |
| **Ingress** | `ingresses/ai-route-litellm-ai-test.internal.yaml` | Routes `Host: ai-test.local` + `/v1` (Prefix) → `llm-litellm.internal.static:80` with retry annotations |
| **McpBridge** | `mcpbridges/default.yaml` | Static service registries: `litellm` and `llm-litellm.internal` → `172.30.50.2:4000`, `higress-console` → `127.0.0.1:8001` |
| **WasmPlugin (auth)** | `wasmplugins/key-auth.internal.yaml` | Consumer `test-client` with Bearer token; enabled on `ai-route-litellm-ai-test.internal`, disabled on `default` |
| **WasmPlugin (AI)** | `wasmplugins/ai-proxy.internal.yaml` | Provider `litellm` → `http://172.30.50.2:4000/v1` with token `sk-local-test`; matched to `llm-litellm.internal.static` |
| **WasmPlugin (stats)** | `wasmplugins/ai-statistics-2.0.1.yaml` | Token usage logger; matched to `ai-route-litellm-ai-test.internal` |
| **EnvoyFilter** | `envoyfilters/higress-http-resolver-cluster.yaml` | Static Envoy cluster for HTTP resolver at `127.0.0.1:8889` |
| **EnvoyFilter** | `envoyfilters/higress-gateway-global-custom-response.yaml` | Custom response handling |
| **Secret** | `secrets/default.yaml`, `secrets/higress-console.yaml` | TLS and console auth secrets |

### 5.6 Monitoring Configuration

**Directory:** `d:\zhilin\API网关\config\monitoring\`

| File | Purpose |
|---|---|
| `prometheus.yml` | Two scrape jobs: `higress` (`127.0.0.1:15000/stats/prometheus`) and `higress-agent` (`127.0.0.1:15020/stats/prometheus`), 15s interval |
| `provisioning/datasources/prometheus.yml` | Registers "Higress Prometheus" (UID `higress-prometheus`) at `http://api-gateway-higress:19090` as Grafana's default datasource |
| `provisioning/dashboards/higress.yml` | Folder provider "API Gateway", reloads every 10s from `/var/lib/grafana/dashboards` |
| `dashboards/higress.json` | 12-panel dashboard definition |

**Dashboard panels:**

| # | Panel | Metric | Purpose |
|---|---|---|---|
| 1 | Scrape status | `up{job=~"higress\|higress-agent"}` | 1=healthy, 0=failed |
| 2 | Gateway uptime | `envoy_server_uptime` | Total uptime since start |
| 3 | Ingress QPS by listener | Rate of `envoy_http_downstream_rq_total` (8080/8443) | Throughput per listener |
| 4 | HTTP status codes | `envoy_http_downstream_rq` by `response_code_class` | 1xx–5xx breakdown |
| 5 | Cumulative requests | Total count on 8080/8443 | Since-gateway-start counter |
| 6 | 5xx error ratio | Percentage of 5xx responses | Server error rate |
| 7 | Upstream request rate | `envoy_cluster_upstream_rq_total` (litellm clusters) | LiteLLM traffic volume |
| 8 | Upstream timeout rate | `envoy_cluster_upstream_rq_timeout` per cluster | Timeout frequency |
| 9 | Upstream retry rate | `envoy_cluster_upstream_rq_retry` per cluster | Retry frequency |
| 10 | Upstream avg latency | `rq_time_sum / rq_time_count` (ms) | Request latency |
| 11 | Active listeners | `envoy_listener_manager_total_listeners_active` | Listener health state |
| 12 | Active requests | `envoy_http_downstream_rq_active` (8080/8443) | In-flight requests |

### 5.7 Config Reload Mechanisms

| Component | Reload Method | Trigger |
|---|---|---|
| Higress (Envoy) | Hot reload via xDS | Controller watches `/data` directory; YAML changes push xDS to Envoy — no restart |
| LiteLLM | Container restart | `config.yaml` mounted read-only; changes require `docker compose restart litellm` |
| Prometheus | File watch + SIGHUP | Watches `prometheus.yml` on disk; edits on host trigger auto-reload |
| Grafana | Provisioning auto-reload | Dashboard provider scans every 10s for new/changed JSON |
| PostgreSQL | Container recreation | Env vars set at container creation |
| LiteLLM patches | Image rebuild | Streaming patches applied at build; requires `docker compose build litellm` |
| LiteLLM callback | Image rebuild | `gateway_responses_compat.py` COPYed at build (not volume-mounted) |

---

## 6. Data Flow Analysis

### 6.1 User Data (Request/Response Payload)

```mermaid
flowchart LR
    subgraph Inbound["Inbound (Client → Provider)"]
        A["Client request body<br/>(model, messages, params)"]
        B["Higress: Host/path routing,<br/>key auth, header rewrite"]
        C["LiteLLM: key validation,<br/>model alias → provider mapping"]
        D["Upstream: API key substitution,<br/>protocol normalization"]
    end

    subgraph Outbound["Outbound (Provider → Client)"]
        E["Upstream: inference result<br/>(JSON or SSE stream)"]
        F["LiteLLM: empty-choices guard,<br/>usage recording to PostgreSQL"]
        G["Higress: token stats collection<br/>(ai_log), response forward"]
    end

    A --> B --> C --> D
    E --> F --> G
```

**Data stored in PostgreSQL:**
- Virtual keys (hashed), associated teams, model permissions, budgets, expiration dates
- `SpendLog` records: request ID, model, token counts (prompt/completion/total), spend amount, timestamp
- `BudgetTable`: periodic budget resets (currently empty in production data)
- `AuditLog`: operation audit trail (currently empty)
- `OrganizationTable`: campus org hierarchy (currently empty)
- `SSOConfig`: SSO provider settings (currently empty)

**Current data state (as of 2026-09-16):**
- 1 user, 1 team
- 6 verification tokens (5 with positive budgets, 4 expired)
- 236 SpendLog entries, **all with spend=0** (tokens counted but cost not attributed)
- No RPM/TPM/concurrency limits on keys or teams

### 6.2 Configuration Data

```mermaid
flowchart TD
    subgraph Source["Configuration Sources"]
        ENV_FILES[".env files (API keys, credentials)"]
        YAML_CONFIGS["config/*.yaml (model routing, monitoring)"]
        RUNTIME_DATA["runtime/higress/*.yaml (ingress, WASM, service registry)"]
        COMPOSE["docker-compose*.yml (service definitions, networks, volumes)"]
        IMAGE["Docker image layers (LiteLLM patches, callback code)"]
    end

    subgraph Consumers["Configuration Consumers"]
        H["Higress controller → Envoy xDS"]
        L["LiteLLM proxy (startup read)"]
        P["Prometheus (file watch)"]
        G["Grafana (provisioning scan)"]
    end

    ENV_FILES --> L
    YAML_CONFIGS --> L
    YAML_CONFIGS --> P
    YAML_CONFIGS --> G
    RUNTIME_DATA --> H
    COMPOSE -->|"container orchestration"| H
    COMPOSE -->|"container orchestration"| L
    IMAGE --> L
```

**Secret handling:**
- `.env` contains real API keys (`DASHSCOPE_API_KEY`, `ZHILIN_aigc_API_KEY`, `DEEPSEEK_API_KEY`, `AGNES_API_KEY`)
- `.env` is excluded from Git via `.gitignore`
- `.env.example` provides only a placeholder for `MODEL_API_KEY`
- Higress consumer key (`3f8f9e70-8a3f-4470-825c-f1f783f72737`) stored in `runtime/higress/wasmplugins/key-auth.internal.yaml`
- LiteLLM master key (`sk-local-test`) hardcoded in `docker-compose.yml` environment block
- PostgreSQL credentials (`llmproxy`/`dbpassword9090`) hardcoded in `docker-compose.yml`

### 6.3 Monitoring Data

```mermaid
flowchart LR
    subgraph Sources["Metric Sources"]
        EP["Envoy data plane<br/>127.0.0.1:15000/stats/prometheus"]
        AP["Higress agent<br/>127.0.0.1:15020/stats/prometheus"]
    end

    subgraph Collection["Collection"]
        P["Prometheus<br/>(shared network namespace with Higress)<br/>scrape every 15s, retain 7d,<br/>listens on 0.0.0.0:19090"]
    end

    subgraph Presentation["Presentation"]
        G["Grafana<br/>queries Prometheus via<br/>http://api-gateway-higress:19090<br/>(Docker DNS resolution)"]
        DASH["12-panel dashboard<br/>auto-provisioned, 10s refresh"]
    end

    EP -->|"localhost scrape"| P
    AP -->|"localhost scrape"| P
    P -->|"0.0.0.0:19090<br/>visible on Docker network"| G
    G --> DASH
```

**Monitoring gaps (not currently covered):**
- LiteLLM has no Prometheus exporter configured — proxy-level metrics (per-model latency, token rates, error counts) are not scraped
- PostgreSQL has no `postgres_exporter` — database health, connection pool, query performance not monitored
- No alerting rules configured (0 alert rules in Prometheus)
- No application-level error tracking (no Loki, no OpenTelemetry tracing)
- No per-key/per-user statistics in the dashboard
- No time-to-first-token (TTFT) metrics

---

## 7. Deployment and Startup Sequence

### 7.1 Service Dependency Graph

```mermaid
graph TD
    DB["1. PostgreSQL (db)<br/>no dependencies"]
    L["2. LiteLLM (litellm)<br/>depends on: db [service_healthy]"]
    H["3. Higress (higress)<br/>depends on: litellm [service_started]"]
    P["4. Prometheus<br/>(separate compose,<br/>network_mode: container:higress)"]
    G["5. Grafana<br/>(separate compose,<br/>joins api-gateway-network)"]

    DB --> L --> H
    H --> P
    H --> G
```

### 7.2 Startup Steps

| Order | Service | Action | Readiness Gate |
|---|---|---|---|
| 1 | **PostgreSQL** | Container starts; initializes database `litellm` with user `llmproxy` | Health check: `pg_isready -U llmproxy -d litellm` (5s interval, 10 retries) |
| 2 | **LiteLLM** | Starts with `--config /app/config.yaml --port 4000`; loads model list, connects to PostgreSQL | No health check defined in main compose (benchmark compose uses `/v1/models` probe) |
| 3 | **Higress** | Reads `/data` directory (mounted from `runtime/higress/`); controller builds xDS config and pushes to Envoy; opens listeners on 8080/8443/8001 | No health check defined in main compose |
| 4 | **Prometheus** | Launched via `docker-compose.monitoring.yml`; shares Higress network namespace; begins scraping `:15000` and `:15020` | None defined |
| 5 | **Grafana** | Launched via `docker-compose.monitoring.yml`; joins `api-gateway-network` at `172.30.50.5`; auto-provisions datasource and dashboard | None defined |

### 7.3 Pre-deployment Requirements

1. Create external Docker volume: `docker volume create litellm_postgres_data`
2. Copy `.env.example` to `.env` and populate with real API keys
3. Ensure `runtime/higress/` directory has valid YAML configs (in case of fresh clone)
4. Build LiteLLM patched image: `docker compose build litellm` (runs 13 build-time tests)

### 7.4 Benchmark Deployment (separate environment)

| Order | Service | Purpose |
|---|---|---|
| 1 | `init` | Copies Higress ingress/McpBridge configs to `runtime/gateway-benchmark/higress/` |
| 2 | `mock` | Starts fake OpenAI upstream on `172.30.51.4:9000` |
| 3 | `litellm` | LiteLLM proxy pointing `benchmark-chat` → mock; on `172.30.51.2:4000` |
| 4 | `higress` | Gateway with two routes: direct-to-mock and full-chain |
| 5 | `k6` | On-demand load testing (not auto-started; `profiles: [tools]`) |

Benchmark network: `172.30.51.0/24` (isolated from main `172.30.50.0/24` stack). All host ports bind to `127.0.0.1` only.

---

## 8. Higress vs LiteLLM — Role Comparison

| Capability | Higress | LiteLLM |
|---|---|---|
| **Layer** | Edge ingress (L7 reverse proxy) | AI model routing (application proxy) |
| **Runtime** | Envoy (C++) + Go controller | Python (FastAPI/Starlette) |
| **TLS termination** | Yes — handles HTTPS on `:8443`, ACME/auto-cert configured | No — receives plaintext HTTP from Higress on internal network |
| **Key authentication** | Yes — via `key-auth` WASM plugin, consumer Bearer tokens, per-ingress enable/disable | Yes — via virtual keys (DB-stored), master key, model-level permissions |
| **Rate limiting** | Not currently configured (no rate-limit WASM plugin registered) | Per-key/per-team via Virtual Key settings (admin UI → DB), not currently configured |
| **Model awareness** | No — routes based on Host + path, not model name | Yes — resolves 8 aliases to 4 providers |
| **Protocol adaptation** | No — transparent HTTP/TCP proxy | Yes — normalizes provider APIs to OpenAI-compatible format |
| **Cost accounting** | Token stats via `ai-statistics` WASM (ingest into Envoy logs only) | Full spend tracking: token counts, cost calculation, `SpendLog` writes to PostgreSQL |
| **Failover/retry** | Yes — `proxy-next-upstream` (3 attempts, 120s, error/timeout/non-idempotent) on AI route | Yes — cross-provider fallback, retry with backoff (configurable per model) |
| **Streaming support** | Transparent passthrough (SSE events flow through Envoy buffers) | Active processing — parses SSE, patches empty choices, normalizes format |
| **WASM plugins** | Yes — `key-auth` (310), `ai-proxy` (100), `ai-statistics` (900) | N/A — uses Python callbacks/hooks instead |
| **Metrics** | Envoy metrics (QPS, latency, errors, upstream health) on `:15000` and `:15020` | No Prometheus exporter; usage data in PostgreSQL only |
| **Configuration** | Kubernetes-style YAML on disk (hot reload via xDS controller) | Single `config.yaml` file (cold reload, container restart) |
| **What it decides** | **Who** can enter and **how** traffic is governed | **Which model** handles the request and **whether** the user is authorized for it |
| **Blind spots** | Never sees model tokens, cost data, or application-level errors | Never handles TLS, IP filtering, or transport-level connection management |

### Critical Separation Principle

> Higress enforces infrastructure-level policies (network access, transport auth). LiteLLM enforces business-level policies (model permissions, budgets, quotas). **Do not let both layers enforce the same token rate limits** — stream retries and fallbacks may double-count. LiteLLM should be the authoritative source for business quotas; Higress only protects at the edge (IP/global burst prevention).

---

## 9. Concrete Request Examples

### 9.1 Path A — Standard Ingress (LiteLLM Virtual Key)

**Hop 1: Client → Higress**

```
POST http://localhost:8080/v1/chat/completions
Content-Type: application/json
Authorization: Bearer sk-litellm-virtual-key-001
Accept: application/json

{
  "model": "my-qwen3.6-27b",
  "messages": [{"role": "user", "content": "What is the capital of France?"}],
  "temperature": 0,
  "max_tokens": 128
}
```

**Hop 2: Higress → LiteLLM** (transparent forward; key-auth disabled on default ingress)

```
POST http://172.30.50.2:4000/v1/chat/completions
Content-Type: application/json
Authorization: Bearer sk-litellm-virtual-key-001

{
  "model": "my-qwen3.6-27b",
  "messages": [{"role": "user", "content": "What is the capital of France?"}],
  "temperature": 0,
  "max_tokens": 128
}
```

**Hop 3: LiteLLM → Upstream Provider** (model alias resolved, provider API key injected)

```
POST https://test2-aigc.campusapp.com.cn/api/v1/chat/completions
Content-Type: application/json
Authorization: Bearer <ZHILIN_aigc_API_KEY>

{
  "model": "Qwen3.6-27B",
  "messages": [{"role": "user", "content": "What is the capital of France?"}],
  "temperature": 0,
  "max_tokens": 128
}
```

**Response: Provider → LiteLLM → Higress → Client**

```
HTTP/1.1 200 OK
Content-Type: application/json
Server: envoy

{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "model": "Qwen3.6-27B",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "The capital of France is Paris."},
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 8,
    "total_tokens": 20
  }
}
```

### 9.2 Path A — Streaming Variant

**Request (same as above but with streaming):**

```json
{
  "model": "my-qwen3.6-27b",
  "messages": [{"role": "user", "content": "What is the capital of France?"}],
  "stream": true
}
```

**Response (SSE stream):**

```
HTTP/1.1 200 OK
Content-Type: text/event-stream
Server: envoy

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"The"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":" capital"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":" of France is Paris."},"finish_reason":"stop"}]}

data: [DONE]
```

> **Note:** The `streaming_iterator` patch (3 guards) processes each chunk server-side to skip empty-choice chunks that would cause `IndexError` on `chunk.choices[0]`.

### 9.3 Path B — AI Route (Higress Consumer Key)

**Hop 1: Client → Higress**

```
POST http://localhost:8080/v1/chat/completions
Host: ai-test.local
Content-Type: application/json; charset=utf-8
Authorization: Bearer 3f8f9e70-8a3f-4470-825c-f1f783f72737

{
  "model": "my-qwen3.6-27b",
  "messages": [{"role": "user", "content": "Hello"}],
  "temperature": 0,
  "max_tokens": 64
}
```

**Hop 2: Higress → LiteLLM** (ai-proxy WASM rewrites auth header and destination)

```
POST http://172.30.50.2:4000/v1/chat/completions
Content-Type: application/json; charset=utf-8
Authorization: Bearer sk-local-test

{
  "model": "my-qwen3.6-27b",
  "messages": [{"role": "user", "content": "Hello"}],
  "temperature": 0,
  "max_tokens": 64
}
```

> **Note:** The consumer's original Bearer token is replaced with the LiteLLM master key. LiteLLM does not know the individual consumer's identity.

**Hop 3:** Same as Path A — LiteLLM resolves model alias and forwards to upstream provider.

### 9.4 `/v1/responses` API (New Endpoint)

**Request:**

```
POST http://localhost:8080/v1/responses
Authorization: Bearer sk-litellm-virtual-key-001
Content-Type: application/json

{
  "model": "my-qwen3.6-27b",
  "instructions": "You are a helpful assistant.",
  "input": [{"role": "user", "content": "What is 2+2?"}]
}
```

**Callback behavior (for `my-qwen3.6-27b` only):** The `gateway_responses_compat` callback merges leading `system`/`developer` messages from the `input` array into the `instructions` field (joined by `\n\n`), preventing Qwen's chat template from rejecting duplicate system messages. For other models and for `/v1/chat/completions`, the callback is a no-op.

### 9.5 Error Responses

| Scenario | Path A Response | Path B Response |
|---|---|---|
| Missing key | `401` (LiteLLM returns unauthorized) | `401` (Higress key-auth blocks before LiteLLM) |
| Invalid key | `401` / `403` (LiteLLM) — currently returns `400` with "No connected db." per test results | `401` / `403` (Higress) |
| Model forbidden | `403` with `key_model_access_denied` | Master key bypasses model permissions — no model-level denial |
| Unknown model | `400` / `404` / `422` (LiteLLM) | Same (passes through Higress auth first) |
| Malformed JSON | `400` / `422` | Same |

### 9.6 Health Check Endpoints

| Endpoint | Auth | Expected Response |
|---|---|---|
| `GET http://localhost:8080/v1/models` | `Bearer sk-local-test` or valid virtual key | 200, JSON array of 8 model aliases |
| `GET http://localhost:4000/v1/models` | `Bearer sk-local-test` | 200, JSON array of 8 model aliases |
| `GET http://localhost:8001/` | None | Higress admin console (HTML) |
| `GET http://localhost:3000/api/health` | None (anonymous access) | Grafana health status |
| `GET http://172.30.50.3:15000/stats/prometheus` | None (internal only) | Prometheus-formatted Envoy metrics |

---

## 10. Known Issues and Open Questions

### 10.1 P0 Issues (Block Delivery)

| # | Issue | Impact | Location |
|---|---|---|---|
| 1 | AI Route uses master key — no per-consumer identity/quota at LiteLLM | Cannot isolate user costs, enforce per-user model permissions, or audit per-user usage on Path B | `ai-proxy.internal.yaml` (token hardcoded as `sk-local-test`) |
| 2 | Hardcoded credentials and wide port binding | `sk-local-test`, `dbpassword9090`, and ports `4000`/`8001`/`8080`/`8443` bound to all interfaces — security exposure | `docker-compose.yml` environment blocks |
| 3 | key-auth plugin FAIL_OPEN mode | Authentication failures may not block requests in all scenarios | `key-auth.internal.yaml` |
| 4 | No root commits, untracked config files, gitlinks without `.gitmodules` | Repository not rebuildable from clone; cannot verify or reproduce environment | Root `.git/`, `sources/*` as gitlinks |
| 5 | Running LiteLLM version (1.100.0) differs from source (1.102.0) | Patch anchors and behavior may not match source; drift risk | `docker-compose.yml` pinned base image SHA vs `sources/litellm-src/` |
| 6 | `.env.example` incomplete — only lists `MODEL_API_KEY` but config uses 4 different env vars | New users cannot set up without reading actual config | `.env.example` vs `config/litellm.yaml` |

### 10.2 P1 Issues (Block Production Readiness)

| # | Issue |
|---|---|
| 1 | Campus SSO, organization sync, and role-based permissions not implemented |
| 2 | Cost reconciliation impossible — all 236 SpendLogs show `spend=0` |
| 3 | RPM/TPM/concurrency limits not configured on any keys or teams |
| 4 | No business-level failover or sticky session |
| 5 | Model capability catalog not independently verified |
| 6 | Monitoring gaps: no LiteLLM metrics, no PostgreSQL exporter, no alerting, no tracing |
| 7 | No model marketplace or self-service portal |
| 8 | Subscription/OAuth and content audit not connected |
| 9 | Two confirmed test false-positives (`test_dual_gateway.py` accepts empty `{}`, `mock.test.mjs` comma expression bug) |
| 10 | Benchmark containers exited with code 137 (potential OOM) — capacity data unreliable |
| 11 | No formal capacity testing or delivery drill |

### 10.3 Open Questions from PRD (Q01–Q12)

The Product Requirements Document (`doc/校园统一AI网关产品需求文档.md`) documents 12 open questions that must be resolved before P0 development begins:

1. **Q01:** Final gateway technology choice (Higress confirmed vs. APISIX alternative)
2. **Q02:** First-batch model and client list
3. **Q03:** Campus identity protocol (CAS, OAuth2, SAML, SCIM)
4. **Q04:** Internal quota units and allocation method
5. **Q05:** Cost allocation between departments
6. **Q06:** Subscription/OAuth scope (user-provided keys vs. institution-purchased)
7. **Q07:** Cross-provider fallback strategy
8. **Q08:** IP allowlist and log retention policies
9. **Q09:** Content audit scope (input, output, both)
10. **Q10:** Capacity targets (concurrent users, RPS, TTFT)
11. **Q11:** Private model deployment boundaries
12. **Q12:** Self-service onboarding flow

---

*This document was generated on 2026-09-16. It reflects the state of the codebase on branch `test` with zero root commits. All file paths are absolute under `d:\zhilin\API网关\`.*
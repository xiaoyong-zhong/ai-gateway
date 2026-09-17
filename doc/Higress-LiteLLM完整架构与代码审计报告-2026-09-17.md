# Higress + LiteLLM AI 网关架构与代码审计报告

核查基准：2026-09-17  
核查范围：当前工作区实际目录、根 Compose 文件、配置与运行快照、补丁代码、测试、历史运行报告、上游源码相关调用位置，以及本机容器只读状态  
安全边界：未展示真实密钥、Token、密码、用户明细或请求正文；未修改业务代码、配置或数据库记录，未部署、未重建容器，未发起真实模型生成请求

本文将项目中的 HiGateway 按实际组件名称统一称为 Higress。sources/higress-src 和 sources/litellm-src 是上游源码目录或 gitlink，不等于项目已经实现了上游的全部能力。

## 一、项目架构现状

### 1.1 项目目标和核心业务场景

项目目标是提供统一 AI API 入口，使客户端使用一个 Base URL 和平台侧凭证调用多个逻辑模型。目标能力包括模型和供应商接入、协议适配、认证、路由、故障处理、Token 与成本统计、监控和审计。

当前核心流程是：客户端访问 Higress 8080/8443，Higress 按 Host/Path 路由并执行部分 WASM 插件，再转发到 LiteLLM 4000。LiteLLM 解析逻辑模型名，调用公有云或校内模型 Provider，并将部分调用数据写入 PostgreSQL。Prometheus 抓取 Envoy 指标，Grafana 展示基础网关看板。

模型配置当前包含 8 个逻辑模型别名，来源为 config/litellm.yaml:1。校园 SSO、组织同步、统一门户、订阅 OAuth、完整配额和正式账单仍未形成项目闭环。

### 1.2 整体架构图

~~~mermaid
flowchart LR
    C[客户端\ncurl / OpenAI SDK / 业务系统 / 工具]
    H[Higress All-in-one\nEnvoy 数据面\n8080 / 8443]
    K[Key Auth WASM\n消费者 Key 校验]
    A[AI Proxy WASM\nProvider 处理]
    R[Ingress + McpBridge\nHost/Path 到服务]
    L[LiteLLM Proxy\nHTTP 4000]
    CB[Responses Callback\n特定模型适配]
    DB[(PostgreSQL 16\nLiteLLM 表与 SpendLogs)]
    CFG[config/litellm.yaml\n静态模型配置]
    P1[DashScope 兼容 API]
    P2[DeepSeek API]
    P3[Agnes API]
    P4[校内模型 API]
    PM[Prometheus\n15 秒抓取，7 天保留]
    G[Grafana\n12 个面板]
    EM[Envoy 指标\n15000 / 15020]

    C -->|HTTP/HTTPS JSON 或 SSE| H
    H --> K
    K --> A
    H --> R
    A --> R
    R -->|HTTP 172.30.50.2:4000| L
    CFG -.只读挂载 /app/config.yaml.-> L
    L --> CB
    L --> P1
    L --> P2
    L --> P3
    L --> P4
    L -->|Prisma/PostgreSQL| DB
    H -.Prometheus HTTP.-> EM
    PM -->|HTTP scrape| EM
    G -->|PromQL HTTP API| PM
~~~

整体编排：docker-compose.yml:1。监控编排：docker-compose.monitoring.yml:1。压测编排：docker-compose.benchmark.yml:1，仅使用 Mock Provider，不属于真实模型链路。

### 1.3 组件、职责、通信和状态

| 组件 | 当前状态 | 职责 | 通信方式 | 关键位置 |
| --- | --- | --- | --- | --- |
| api-gateway-higress | 当前运行中 | 外部入口、Envoy 路由、WASM 插件 | HTTP/HTTPS；内部 HTTP | docker-compose.yml:50 |
| api-gateway-litellm | 当前运行中 | AI API 代理、模型别名、Provider 调用、部分计量 | HTTP 4000；HTTPS Provider；PostgreSQL | docker-compose.yml:24 |
| litellm_db | 当前运行中，数据库 health 为 healthy | Key、User、Team、SpendLogs 持久化 | PostgreSQL 协议 | docker-compose.yml:2 |
| Prometheus | 当前运行中 | 抓取 15000/15020 指标并保存 | HTTP scrape | docker-compose.monitoring.yml:2 |
| Grafana | 当前运行中 | PromQL 查询和可视化 | HTTP 3000，本机回环绑定 | docker-compose.monitoring.yml:17 |
| AI Proxy WASM | runtime 已配置 | 部分 AI Provider 请求头和协议处理 | Envoy HTTP filter | runtime/higress/wasmplugins/ai-proxy.internal.yaml:53 |
| Key Auth WASM | runtime 已配置 | 专用路由消费者 Key 校验 | Envoy HTTP filter | runtime/higress/wasmplugins/key-auth.internal.yaml:63 |
| AI Statistics WASM | runtime 已配置 | 统计插件配置快照 | Envoy HTTP filter | runtime/higress/wasmplugins/ai-statistics-2.0.1.yaml:46 |
| runtime/higress | 当前挂载，被 Git 忽略 | Ingress、McpBridge、WASM 运行快照 | Higress 挂载 /data | docker-compose.yml:60 |
| Redis | 未在当前 Compose 声明 | 无法确认缓存或共享限流状态 | 无 | docker-compose.yml:1 |
| 校园管理门户 | 未发现本项目独立实现 | SSO、组织、申请、目录和帮助 | 待确认 | doc/校园统一AI网关产品需求文档.md:238 |

当前主服务端口是 4000、8001、8080、8443，均在主 Compose 中绑定到所有主机接口。Grafana 3000 绑定 127.0.0.1。LiteLLM 和 Higress 没有 Docker healthcheck，容器 Up 不等于业务 ready。

### 1.4 两条实际入口路径

| 路径 | 匹配规则 | 认证 | 下游身份 | 结论 |
| --- | --- | --- | --- | --- |
| 默认代理路径 | Higress 默认域名，路径 /v1 | LiteLLM 侧认证 | 客户端提交的 LiteLLM Key | 更接近 LiteLLM 原生代理路径 |
| 专用测试路径 | Host=ai-test.local，路径 /v1 | Higress Key Auth | AI Proxy 固定上游 Token | 固定 Token 与 LiteLLM 主密钥相同，不能直接代表消费者独立身份 |

专用路由配置在 runtime/higress/ingresses/ai-route-litellm-ai-test.internal.yaml:46，AI Proxy Provider 在 runtime/higress/wasmplugins/ai-proxy.internal.yaml:53。通过只读配置比对确认其 apiTokens 与 Compose 中的 LiteLLM 主密钥相等，真实值未展示。

## 二、Higress 在本项目中的能力边界

Higress 当前是外层入口、Envoy 路由和插件承载层。它不是当前项目的预算账本或校园管理后台。

| Higress 能力 | 原生是否支持 | 当前是否接入 | 证据 | 当前作用 | 问题 |
| --- | --- | --- | --- | --- | --- |
| API 网关与统一入口 | 支持 | 已接入且已验证 | docker-compose.yml:56 | 监听 8080/8443 | 端口宽绑定 |
| 域名与路径路由 | 支持 | 已接入且目录 API 已验证 | runtime/higress/ingresses/ai-route-litellm-ai-test.internal.yaml:48 | Host 和 /v1 匹配 | runtime 被 Git 忽略 |
| 反向代理 | 支持 | 已接入且目录 API 已验证 | runtime/higress/mcpbridges/default.yaml:23 | 转发到 LiteLLM 4000 | 真实 POST 本次未发起 |
| TLS/HTTPS 终止 | 支持 | 端口和快照存在，未验证 | docker-compose.yml:59 | 预留 8443 | 证书、域名和强制 HTTPS 待验证 |
| API Key 认证 | 支持 | 专用路由已接入 | runtime/higress/wasmplugins/key-auth.internal.yaml:63 | 消费者认证 | failStrategy=FAIL_OPEN |
| JWT/OAuth | 可扩展 | 未接入校园身份 | runtime 未发现业务配置 | 理论上可扩展 | SSO/OAuth 未打通 |
| IP 黑白名单 | 可扩展 | 当前未确认启用 | 未发现专用路由 IP 规则 | 理论上做来源控制 | 可信代理链和 CIDR 规则待验证 |
| 限流 | 可通过插件实现 | 未证明已启用 | 当前 Compose 无 Redis；schema 只说明 LiteLLM 字段 | 入口保护的候选层 | 没有 Key/IP/Token 限流证据 |
| 熔断/超时/重试 | 支持 | 部分配置 | runtime/higress/ingresses/ai-route-litellm-ai-test.internal.yaml:7-11 | 网络级 next-upstream | 可能和 LiteLLM 重试叠加 |
| CORS | 支持 | 当前未发现业务配置 | runtime 未发现专用 CORS | 理论上可配置 | 浏览器预检待验证 |
| 请求头改写 | 支持 | AI Proxy 已接入 | sources/higress-src/plugins/wasm-go/extensions/ai-proxy/provider/openai.go:147 | 处理上游 Authorization | 专用路径固定主密钥 |
| 响应改写 | 可扩展 | 仅部分 AI 适配 | runtime/higress/wasmplugins/ai-proxy.internal.yaml:53 | 由插件处理部分请求 | 不是全协议保证 |
| WASM 插件 | 支持 | Key Auth、AI Proxy、Statistics 快照存在 | runtime/higress/wasmplugins | 认证、AI 代理、统计 | 插件 URL/版本需要复验 |
| 灰度/权重 | 支持路由策略 | 当前未确认使用 | 未发现已生效灰度规则 | 理论上可用 | 没有发布/回滚证据 |
| Envoy 日志和指标 | 支持 | 基础指标已接入 | config/monitoring/prometheus.yml:5 | QPS、状态码、上游等 | 没统一审计和告警 |
| 多租户 | 可组合实现 | 未完成 | 专用消费者只有测试身份 | 理论上隔离 | 下游固定主密钥破坏身份边界 |
| AI 协议适配 | AI Proxy 支持部分 | 部分接入 | runtime/higress/wasmplugins/ai-proxy.internal.yaml:53 | 处理部分 OpenAI Provider | LiteLLM 才是模型代理核心 |

Higress 是否直接理解 OpenAI Chat Completion：AI Proxy 能理解并处理部分 AI 请求，但当前模型别名、Provider 选择和主要响应转换由 LiteLLM 完成。Higress 当前没有证据表明它负责用户预算、成本账本或多 Provider 业务 Fallback。

## 三、LiteLLM 在本项目中的能力边界

LiteLLM 是当前真实的 AI 模型代理核心。上游源码提供的原生能力不能直接等同于当前项目已启用。

| LiteLLM 能力 | 原生是否支持 | 当前是否启用 | 证据 | 实际效果 | 未完成部分 |
| --- | --- | --- | --- | --- | --- |
| OpenAI Chat | 支持 | 已配置，历史有真实调用 | config/litellm.yaml:1；test/test_gateway_models.py:367 | 逻辑模型可映射 Provider | 当前版本真实 POST 待复验 |
| Responses | 支持 | 部分启用 | config/litellm-patch/gateway_responses_compat.py:38 | 特定模型前置指令适配 | 原生 Responses/工具/状态复用不保证 |
| Embeddings | 支持 | 配置声明，历史有记录 | config/litellm.yaml:23 | 可调用向量接口 | 当前版本和语义质量待复验 |
| Images | 支持 | 配置声明，未做本次真实调用 | config/litellm.yaml:50 | 理论上调用图片接口 | 生成、费用和保存未验收 |
| Audio/Rerank | Provider 相关支持 | 未配置 | model_list 无对应条目 | 当前无效果 | 需要单独接入 |
| 多 Provider | 支持 | 已配置 4 类端点 | config/litellm.yaml:2 | 8 个别名可列出 | 余额、权限、接口能力未全部确认 |
| 模型别名 | 支持 | 已启用 | config/litellm.yaml:29 | /v1/models 返回别名 | qwen3.7-flash 映射存在疑点 |
| 多 deployment 负载均衡 | 支持 | 未启用 | 每个别名只有一个配置 | 当前没有同模型多实例均衡 | 需要多 deployment 和权重 |
| Fallback | 支持 | 当前未启用 | AI Proxy failover=false；无多 deployment | 没有业务级切换证据 | 需定义兼容池和成本规则 |
| 重试 | 支持 | 配置分裂 | Higress next-upstream 3 次；benchmark LiteLLM 重试为 0 | 可能发生网络重试 | 正式 LiteLLM 重试边界待确认 |
| Virtual Key | 支持 | 数据库有 Key 记录 | sources/litellm-src/schema.prisma:419 | 现有 6 条 VerificationToken | 生命周期和权限交集未验收 |
| User/Team/Project | 支持 | 数据模型存在 | sources/litellm-src/schema.prisma:120、236 | 当前 1 用户/1 团队 | 校园 SSO/组织同步未接入 |
| 预算和预占 | 支持 | 代码和字段存在 | sources/litellm-src/litellm/proxy/spend_tracking/budget_reservation.py:199；sources/litellm-src/schema.prisma:13 | 可执行原生预算路径 | 当前费用快照为 0，未完成对账 |
| RPM/TPM/并发 | 支持 | 当前没有有效配置 | sources/litellm-src/schema.prisma:435 | 字段存在 | 当前 Key/Team 限制为 0 |
| Token 统计 | 支持 | 部分启用 | sources/litellm-src/schema.prisma:628 | 历史 SpendLogs 有 Token | 当前未做 usage 与账本对账 |
| 成本统计 | 支持 | 未证明有效 | SpendLogs spend 字段、模型价格依赖 | 记录存在但当前 spend 为 0 | 价格版本、币种、失败费用待定 |
| 请求日志 | 支持 | 部分启用 | SpendLogs request_id、model、user 等 | 具备部分记录 | 没有跨层 attempt 查询面 |
| 预算告警 | 支持/扩展 | 未启用 | Prometheus rule_groups=0 | 无当前告警证据 | 阈值和通知未配置 |
| 模型权限 | 支持 | 部分字段存在 | VerificationToken.models/permissions | 原生可按 Key 授权 | 专用路径固定主密钥绕过消费者身份 |
| 回调/Hook | 支持 | 已启用一项 | config/litellm.yaml:59 | Responses hook 生效于目标范围 | 不是统一审计/内容审查 |
| PostgreSQL 持久化 | 支持 | 已接入 | docker-compose.yml:34；sources/litellm-src/schema.prisma:628 | 表和历史记录存在 | 备份、恢复、迁移未演练 |
| Prometheus 指标 | 支持 | 当前主要采集 Higress | config/monitoring/prometheus.yml:5 | Envoy 指标可视化 | LiteLLM 业务指标未入当前看板 |
| 管理 API | 支持 | LiteLLM 进程提供基础 API | sources/litellm-src/litellm/proxy/proxy_server.py:10307 | /v1/models 可用 | 统一校园门户未接入 |
| 流式 SSE | 支持 | 已接入部分 | config/litellm-patch/patch_streaming.py:14；test/test_higress_route.py:47 | 历史和离线测试有成功 | 当前版本长流/断流/取消待验证 |
| 缓存 | 支持扩展 | 未接入 | Compose 无 Redis，配置无后端 | 无生产缓存证据 | 命中计量和共享状态未设计 |

LiteLLM 当前模型配置是静态 YAML 为主，不是数据库动态模型配置。Compose 只读挂载 config/litellm.yaml 到 /app/config.yaml。LiteLLM_ProxyModelTable 当前为空，但 /v1/models 仍能从 YAML 返回 8 个别名。

## 四、Higress 与 LiteLLM 职责边界

| 功能领域 | Higress | LiteLLM | 当前项目实际负责方 | 说明 |
| --- | --- | --- | --- | --- |
| 外部流量入口 | 强 | 可监听 HTTP | Higress | 客户端到 8080/8443 |
| 域名和路径路由 | 强 | 有 API 路由 | Higress | Ingress/McpBridge |
| API 认证 | Key Auth/JWT/WASM | Master/Virtual Key | 两层均可能 | 专用路径先 Higress，再固定主密钥 |
| 模型路由 | AI Proxy 可做部分 | 核心能力 | LiteLLM | 当前单 deployment |
| 多供应商适配 | 部分 | 核心强项 | LiteLLM | Provider 配置在 YAML |
| 限流 | 入口/IP/连接 | Key/User/Team/Model | 尚未统一 | 当前数据未配置 |
| 重试/Fallback | Envoy next-upstream | LiteLLM retries/fallback | 配置分裂 | 业务 Fallback 未启用 |
| Token 统计 | 插件/Envoy 指标 | SpendLogs/usage | LiteLLM 底座 | 未形成统一账本 |
| 成本统计 | 不适合作主账本 | 原生支持 | LiteLLM 理论负责 | spend 当前为 0 |
| 请求日志 | Envoy 访问日志 | SpendLogs | 两侧分开 | 缺统一查询 |
| 监控指标 | Envoy 15000/15020 | 可提供 AI 指标 | Prometheus 当前采 Higress | Grafana 偏网络指标 |
| 灰度发布 | 路由权重 | 模型组策略 | 尚未接入 | 无发布和回滚验收 |
| 用户团队 | Consumer 概念 | User/Team/Key | LiteLLM 数据模型 | 校园组织未接入 |
| 数据库存储 | runtime 快照 | Prisma/PostgreSQL | LiteLLM | runtime 被忽略 |
| 流式响应 | 转发 SSE | 生成/适配 SSE | LiteLLM 生成，Higress 转发 | 长连接待联合验收 |
| 安全防护 | 外层网络和插件 | 模型权限和预算 | 分层 | 固定主密钥是核心风险 |

为什么同时使用：Higress 解决边缘入口和网络治理，LiteLLM 解决模型 Provider 和 AI 计量。移除 Higress 会失去当前 Envoy 入口、Host/Path 路由、WASM 插件和边缘指标；移除 LiteLLM 会失去当前模型别名、Provider 适配、AI 代理和 SpendLogs/预算内核。

重复配置主要是认证、重试和限流。应只让一个组件负责业务预算和 Token 扣减，建议 LiteLLM；Higress 只负责入口/IP/连接保护。业务 Fallback 也应指定唯一主控层，当前尚未决定。

当前已确认的冲突风险：专用路径使用共同主密钥；Higress 设有 3 次网络级重试而 LiteLLM 业务 Fallback 未启用；Prometheus 和 SpendLogs 两套数据源没有统一业务查询层。是否已发生双层重试放大，待通过故障注入验证。

## 五、当前已实现功能

状态定义：已实现且已验证表示本次存在对应检查；已实现但未验证表示代码/配置存在但本次未跑真实路径；部分实现表示只有底座或部分流程；配置声明但运行未证明表示文件存在但没有生效证据；仅依赖原生能力表示上游有但本项目未接入；尚未实现表示当前没有项目闭环；无法确认表示证据不足。

| 功能模块 | 实现状态 | 使用组件 | 关键文件/代码 | 配置入口 | 验证方式 | 当前限制 |
| --- | --- | --- | --- | --- | --- | --- |
| Compose 服务编排 | 已实现且已验证（语法） | Docker Compose | docker-compose.yml:1 | 3 个 Compose 文件 | compose config 通过 | 冷启动和复建未验 |
| Higress 启动 | 已实现且已验证（进程层） | Higress | docker-compose.yml:50 | all-in-one + runtime | 容器运行，Envoy /ready LIVE | latest，无 Docker healthcheck |
| LiteLLM 启动 | 已实现且已验证（进程层） | LiteLLM | docker-compose.yml:24 | 补丁镜像 + YAML | liveness 200 | 运行版与源码版不一致 |
| Provider 配置 | 部分实现 | LiteLLM | config/litellm.yaml:2 | model_list | /v1/models 返回 8 别名 | 可用性/额度未全验 |
| OpenAI Chat | 已实现但未验证（本次真实调用） | Higress + LiteLLM | test/test_gateway_models.py:367 | /v1/chat/completions | 历史报告有成功 | 当前版本待复验 |
| Key 认证 | 已实现且已验证（目录） | Key Auth + LiteLLM | runtime/higress/wasmplugins/key-auth.internal.yaml:81 | Consumer/Master Key | 无效 Key 401，合法目录 200 | POST 错误语义待验 |
| 路由规则 | 已实现且已验证（目录） | Higress | runtime/higress/ingresses/ai-route-litellm-ai-test.internal.yaml:48 | Host + /v1 | 合法路由目录 200 | runtime 不在 Git |
| 多模型和别名 | 已实现且已验证（目录） | LiteLLM | config/litellm.yaml:1 | 8 条 model_list | 8 个模型 ID 返回 | 不等于能力全通过 |
| Fallback | 尚未实现（当前业务配置） | 两层网关 | runtime/higress/wasmplugins/ai-proxy.internal.yaml:57 | failover=false | 未做故障注入 | 无业务级切换 |
| 重试 | 部分实现 | Higress | runtime/higress/ingresses/ai-route-litellm-ai-test.internal.yaml:7 | next-upstream 3 次 | 配置存在 | 与 LiteLLM 边界未定 |
| 限流 | 配置声明但运行未证明 | LiteLLM/Higress | sources/litellm-src/schema.prisma:435 | 当前限制字段 0 | 历史 429 只能说明某层限额 | 无共享状态证据 |
| 超时 | 部分实现 | Higress/LiteLLM/脚本 | runtime/higress/ingresses/ai-route-litellm-ai-test.internal.yaml:9 | 120 秒网络配置 | 脚本有 timeout | 总时限/idle timeout 未统一 |
| 流式输出 | 已实现且已验证（离线/历史） | LiteLLM + Higress | config/litellm-patch/patch_streaming.py:14 | stream=true | 补丁 6 项、历史 SSE | 真实长流/取消待验 |
| Token 统计 | 部分实现 | LiteLLM/PostgreSQL | sources/litellm-src/schema.prisma:628 | SpendLogs | 数据库有历史 Token | 未完成账本核对 |
| 成本统计 | 部分实现 | LiteLLM | SpendLogs spend | model cost map | 当前费用快照为 0 | 价格和计费口径未定 |
| 日志记录 | 部分实现 | Envoy + LiteLLM | test/test_gateway_resilience.py:110 | 容器日志/SpendLogs | 数据库和报告可读 | 无统一审计/留存 |
| 数据库持久化 | 已实现且已验证（数据存在） | PostgreSQL | docker-compose.yml:10 | 外部卷 | 只读 SQL 查询成功 | 备份恢复未验证 |
| Redis/缓存 | 尚未实现 | 无 | docker-compose.yml:1 | 无 | 未发现服务 | 多副本共享状态缺失 |
| Prometheus | 已实现且已验证（基础） | Prometheus | config/monitoring/prometheus.yml:5 | 15000/15020 | targets up、查询成功 | 只覆盖 Envoy |
| Grafana | 已实现且已验证（基础） | Grafana | config/monitoring/dashboards/higress.json:1 | 12 面板 | Dashboard API 200 | 无费用/逐 Key 看板 |
| 管理后台 | 尚未实现（校园统一门户） | 未发现项目自研前端 | 上游 UI 源码存在 | Compose 未声明门户 | 未做页面验收 | 上游 UI 不等于集成 |
| 用户/团队 | 部分实现 | LiteLLM | sources/litellm-src/schema.prisma:120、236 | DB 有 1 用户/1 团队 | 只读统计 | SSO/组织同步未接 |
| 权限控制 | 部分实现 | Key Auth + LiteLLM | Key models/permissions | 目录认证通过 | 专用路径身份越权风险 |
| 健康检查 | 部分实现 | DB/LiteLLM/Higress | docker-compose.yml:12 | db、liveness、/ready | 本次手工检查通过 | Compose healthcheck 不完整 |
| 自动重启 | 已配置但未验证 | Compose | docker-compose.yml:5 | unless-stopped | inspect 可见 | 未做故障恢复演练 |
| HTTPS | 配置声明，未验证 | Higress | docker-compose.yml:59 | 8443 | 未做证书握手 | 不能宣称已上线 |
| CORS | 尚未确认 | Higress/LiteLLM | 未发现配置 | 无 | 未做预检 | 待安全评审 |
| 测试用例 | 已实现且已验证（限定范围） | Python/Node | test/test_dual_gateway_unit.py:12 | 单测/Mock/k6 | Python 13、Mock 4、补丁 6+7 通过 | 已复现两项漏检 |
| 部署文档 | 部分实现 | Markdown | doc/gateway-capacity-benchmark.md:21 | 启动/压测说明 | 文档可读 | runtime 不可独立复建 |
| 故障排查文档 | 部分实现 | Markdown/CLI | doc/gateway-resilience-validation.md:80 | 异常和注入方法 | 历史报告存在 | 故障注入未完成 |

历史运行报告不等于当前版本验收。历史记录中 Qwen 并发 10、50 请求曾全部有效，Kimi 并发 60、500 请求出现 100 个 429，且有内容校验失败。文件位置为 runtime/gateway-checks/check-955b5a77b44c40e5aeab14c00ed5dfda.json 和 runtime/gateway-checks/check-c454f490009f4c7194a24b19ae2c1736.json。完整文件名与统计可在目录中核对；本报告不把它们解释为当前容量结论。

## 六、完整请求链路分析

### 6.1 实际请求示例

~~~http
POST http://localhost:8080/v1/chat/completions HTTP/1.1
Host: ai-test.local
Authorization: Bearer <HIGRESS_CONSUMER_KEY>
Content-Type: application/json
Accept: application/json
X-Request-ID: audit-example-001

{
  "model": "my-qwen3.6-27b",
  "messages": [
    {"role": "user", "content": "请介绍一下当前项目的架构。"}
  ],
  "stream": false,
  "max_tokens": 128
}
~~~

构造代码在 test/test_higress_route.py:133。占位符不是真实 Key。

### 6.2 请求处理顺序

| 顺序 | 组件 | 处理 |
| --- | --- | --- |
| 1 | 客户端 | 生成 HTTP POST 和 JSON |
| 2 | Higress Envoy | 接收 8080，匹配 Host 和 /v1 |
| 3 | Key Auth WASM | 校验消费者 Key，失败直接 401 |
| 4 | AI Proxy WASM | 选择 litellm Provider，处理上游 Authorization |
| 5 | McpBridge | 将静态服务名解析到 172.30.50.2:4000 |
| 6 | LiteLLM | 校验收到的 Key，解析模型别名 |
| 7 | LiteLLM | 使用 api_base/api_key 选择实际 Provider |
| 8 | Provider | 接收 LiteLLM 产生的 OpenAI 兼容请求 |
| 9 | LiteLLM | 转换响应、处理 usage |
| 10 | PostgreSQL | 写入 SpendLogs 等记录，提交时机待验证 |
| 11 | Higress | 沿原连接返回；SSE 时转发事件 |
| 12 | 客户端 | 读取 choices、usage、finish_reason |

### 6.3 非流式时序图

~~~mermaid
sequenceDiagram
    autonumber
    participant C as 客户端
    participant H as Higress Envoy 8080
    participant K as Key Auth WASM
    participant A as AI Proxy WASM
    participant R as Ingress/McpBridge
    participant L as LiteLLM 4000
    participant D as PostgreSQL
    participant P as 校内模型 Provider

    C->>H: POST /v1/chat/completions
    H->>K: Envoy filter
    K->>K: 校验消费者 Key
    K-->>H: allow 或 401
    H->>A: 选择 litellm Provider
    A->>A: 重写上游 Authorization
    A->>R: Host/Path 路由
    R->>L: HTTP POST /v1/chat/completions
    L->>L: 校验 Key，解析别名
    L->>P: HTTPS POST 实际模型
    P-->>L: JSON completion + usage
    L->>D: 写入 SpendLogs
    L-->>R: OpenAI 兼容 JSON
    R-->>H: HTTP response
    H-->>C: 200 JSON
~~~

### 6.4 Provider 侧请求

my-qwen3.6-27b 在 config/litellm.yaml:29 映射为 openai/Qwen3.6-27B，Provider base 是配置的校内 /api/v1 端点，Provider Key 从环境变量读取，use_chat_completions_api 为 true。实际 Provider 请求正文本次没有抓取，字段细节属于待验证。

### 6.5 响应示例及字段来源

下面是项目测试链路校验的 OpenAI 响应结构示例。它展示接口字段，不代表本次重新调用真实模型：

~~~json
{
  "id": "chatcmpl-example",
  "object": "chat.completion",
  "created": 1789090000,
  "model": "my-qwen3.6-27b",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "当前项目由 Higress 接收请求，再由 LiteLLM 调用模型。"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 18,
    "completion_tokens": 18,
    "total_tokens": 36
  }
}
~~~

| 字段 | 来源 |
| --- | --- |
| id | Provider 或 LiteLLM 生成/转换 |
| object | LiteLLM OpenAI 兼容层 |
| created | Provider/LiteLLM 时间字段 |
| model | LiteLLM 逻辑模型或 Provider 结果 |
| message.content | Provider 生成，LiteLLM 转换 |
| finish_reason | Provider 结果经 LiteLLM 转换 |
| usage | Provider usage 或 LiteLLM 统计/估算 |
| HTTP 状态码 | LiteLLM 或 Envoy |
| X-Request-ID | 客户端提交或服务端生成/覆盖，当前传递规则待验证 |
| Higress 响应正文 | 当前没有证据表明会修改模型正文，通常负责转发 |

## 七、流式请求链路

### 7.1 示例请求

~~~http
POST http://localhost:8080/v1/chat/completions HTTP/1.1
Host: ai-test.local
Authorization: Bearer <HIGRESS_CONSUMER_KEY>
Content-Type: application/json
Accept: text/event-stream

{
  "model": "my-qwen3.6-27b",
  "messages": [{"role": "user", "content": "请分两段说明网关链路"}],
  "stream": true,
  "stream_options": {"include_usage": true}
}
~~~

### 7.2 时序图

~~~mermaid
sequenceDiagram
    participant C as 客户端
    participant H as Higress
    participant L as LiteLLM
    participant P as Provider

    C->>H: POST stream=true
    H->>L: HTTP 长连接转发
    L->>P: HTTPS streaming request
    P-->>L: data: chunk 1
    L-->>H: data: chunk 1
    H-->>C: data: chunk 1
    P-->>L: content chunks
    L-->>H: content chunks
    H-->>C: content chunks
    P-->>L: finish_reason + usage
    L-->>H: final chunk
    H-->>C: final chunk + data: [DONE]
~~~

### 7.3 当前实现与限制

客户端脚本按 SSE 空行分隔事件，位置 test/test_higress_route.py:47。LiteLLM 补丁在 config/litellm-patch/patch_streaming.py:14 对空 choices 的 usage chunk 做边界保护。历史和离线测试证明过部分 SSE 正常路径；本次未重新验证真实长流、Provider 断流、客户端取消、上游资源释放、重试和部分响应恢复。

客户端应以 Content-Type、非空 delta、finish_reason 和 data: [DONE] 共同判断成功。HTTP 200 单独不能证明流式成功。已输出部分正文后不应直接拼接备用模型正文；当前生产配置没有完成该故障边界验收。

## 八、配置链路

### 8.1 配置加载关系

~~~mermaid
flowchart TD
    DC[docker-compose.yml]
    ENV[根目录 .env]
    EX[.env.example]
    LY[config/litellm.yaml]
    PD[config/litellm-patch/Dockerfile]
    RH[runtime/higress]
    MC[docker-compose.monitoring.yml]
    PP[config/monitoring/prometheus.yml]
    GD[config/monitoring/provisioning + dashboards]
    DB[(litellm_postgres_data)]
    L[LiteLLM]
    H[Higress]
    P[Prometheus]
    G[Grafana]

    EX -.手工复制和填写.-> ENV
    DC -->|命令、端口、卷、网络| L
    ENV -->|可选 env_file| L
    LY -->|只读挂载 /app/config.yaml| L
    PD -->|构建补丁镜像| L
    DC -->|挂载 /data| RH
    RH --> H
    DC --> DB
    MC --> P
    MC --> G
    PP -->|只读挂载| P
    GD -->|只读挂载| G
~~~

### 8.2 配置项

| 配置 | 文件 | 读取方 | 作用 | 是否必填 | 修改后 | 主要风险 |
| --- | --- | --- | --- | --- | --- | --- |
| db image | docker-compose.yml:3 | Docker/PostgreSQL | PostgreSQL 16 | 是 | 重建/启动 | 未固定 digest |
| POSTGRES_DB/USER | docker-compose.yml:7-8 | PostgreSQL | 数据库初始化 | 是 | 新卷初始化 | 结构信息固化 |
| POSTGRES_PASSWORD | docker-compose.yml:9 | PostgreSQL | 数据库密码 | 是 | 需改密流程 | 硬编码敏感值 |
| litellm build | docker-compose.yml:25-28 | Docker | 构建补丁镜像 | 是 | 重建镜像 | 运行版需和源码对齐 |
| command | docker-compose.yml:33 | LiteLLM | 读取 /app/config.yaml，监听 4000 | 是 | 重启 | 无 healthcheck |
| LITELLM_MASTER_KEY | docker-compose.yml:35 | LiteLLM | 主密钥 | 是 | 重启 | 专用路由使用相同值 |
| DATABASE_URL | docker-compose.yml:36 | LiteLLM | Prisma 数据库连接 | 是 | 重启 | URL 可能含凭证 |
| env_file | docker-compose.yml:37-39 | Compose | 可选加载 .env | 模型调用需要 | 重启 | 示例变量不匹配 |
| model_list | config/litellm.yaml:1 | LiteLLM | 模型、Provider、端点 | 是 | 通常重启 | 没有正式发布版本 |
| api_key 引用 | config/litellm.yaml:6 等 | LiteLLM | 从环境取 Provider Key | 对应模型必填 | 重启 | 缺失时模型失败 |
| master_key 引用 | config/litellm.yaml:56 | LiteLLM | 读取主密钥 | 是 | 重启 | 需和 Compose 一致 |
| drop_params | config/litellm.yaml:60 | LiteLLM | 丢弃不支持参数 | 否 | 重启 | 可能静默改变语义 |
| callbacks | config/litellm.yaml:61 | LiteLLM | 加载 Responses hook | 否 | 重启 | 模块/版本不匹配会启动失败 |
| Ingress Host/Path | runtime/higress/ingresses | Higress | 路由 | 路由必需 | 控制面重载/分发 | runtime 被忽略 |
| McpBridge static registry | runtime/higress/mcpbridges/default.yaml:17 | Higress | 服务名到 172.30.50.2:4000 | 路由必需 | 控制面重载 | 依赖固定 IP |
| WASM url/config | runtime/higress/wasmplugins | Higress | 加载插件 | 专用路由必需 | 重新分发 | 插件版本和 URL |
| Prometheus scrape | config/monitoring/prometheus.yml:5 | Prometheus | 抓取 Envoy | 监控必需 | 重载/重启 | 不含 LiteLLM 业务指标 |
| Grafana datasource | config/monitoring/provisioning/datasources | Grafana | 指定 Prometheus | 看板必需 | 重载/重启 | 依赖 network_mode |
| 外部数据库卷 | docker-compose.yml:73-76 | Docker | 数据持久化 | 是 | 首次需预创建 | 新机器不能自动复建 |

### 8.3 环境变量

.env.example 当前只有 MODEL_API_KEY，占位模板与实际 YAML 不一致。config/litellm.yaml 实际引用 DASHSCOPE_API_KEY、ZHILIN_aigc_API_KEY、DEEPSEEK_API_KEY、AGNES_API_KEY。根目录 .env 当前存在这些变量，但值是否有效、Provider 是否有余额和权限，待验证。

LITELLM_MASTER_KEY 和 DATABASE_URL 当前由 Compose environment 定义。敏感值不应放入 Git、日志、前端或示例请求。根 .gitignore 忽略 .env 和 runtime，位置 .gitignore:1，这既保护了秘密，也造成可复建性缺口。

### 8.4 路由规则如何生效

1. Higress 挂载 runtime/higress 到 /data。
2. 控制面读取 Ingress、McpBridge、WasmPlugin。
3. Ingress 按 Host=ai-test.local、Path=/v1 匹配。
4. McpBridge 把服务名解析为 172.30.50.2:4000。
5. WASM matchRules 将 Key Auth、AI Proxy 和 Statistics 作用于相应路由。
6. 控制面生成 Envoy Listener、Route、Cluster 和 WASM filter。
7. Envoy 使用已生效配置。

这些 YAML 是运行状态快照，修改文件不等于 Envoy 已热更新。必须检查运行中的 Envoy 配置或在隔离环境重载验证。

## 九、部署与启动链路

### 9.1 docker compose up -d

~~~mermaid
flowchart TD
    U[docker compose up -d]
    U --> N[创建 api-gateway-network]
    U --> V[检查外部 litellm_postgres_data]
    N --> DB[启动 litellm_db]
    DB --> Q{pg_isready 健康}
    Q -->|否| WAIT[继续等待]
    WAIT --> Q
    Q -->|是| L[启动 api-gateway-litellm]
    L --> H[启动 api-gateway-higress]
    H --> E[加载 runtime 和 Envoy]
    E --> R[人工检查 /ready、liveness、/v1/models、受控 POST]
~~~

depends_on 关系：

- LiteLLM 等待 db 的 service_healthy，位置 docker-compose.yml:30。
- Higress 仅 depends_on litellm，位置 docker-compose.yml:54。
- LiteLLM 和 Higress 没有 Docker healthcheck，Higress 启动不代表 LiteLLM HTTP 4000 已 ready。
- db healthcheck 是 pg_isready，间隔 5 秒、超时 5 秒、重试 10 次，位置 docker-compose.yml:12-16。
- 它只证明 PostgreSQL 接受连接，不证明 Prisma migration、模型 Provider、插件加载或真实请求正常。

### 9.2 监控和压测启动

监控 Compose 使用 network_mode: container:api-gateway-higress 访问 Envoy 回环指标，Grafana 加入外部 api-gateway-network，见 docker-compose.monitoring.yml:6-39。Higress 容器必须先存在。

压测 Compose 使用 172.30.51.0/24，init 复制测试路由，mock 提供固定 JSON/SSE，benchmark LiteLLM 连接 mock，k6 选择 mock/litellm/higress/full。它不访问真实模型，不能证明生产容量。

### 9.3 非破坏性验证命令

~~~powershell
docker compose ps
docker compose config --quiet
docker inspect litellm_db
Invoke-RestMethod http://localhost:4000/health/liveliness
docker exec api-gateway-higress curl -fsS --max-time 5 http://127.0.0.1:15000/ready
Invoke-RestMethod http://localhost:3000/api/health
Invoke-RestMethod http://localhost:3000/api/dashboards/uid/higress-local
docker compose logs --since 10m litellm
docker compose logs --since 10m higress
~~~

模型目录需要使用脱敏受限 Key：

~~~powershell
$headers = @{ Authorization = "Bearer <REDACTED_KEY>" }
Invoke-RestMethod -Uri http://localhost:8080/v1/models -Headers $headers
~~~

### 9.4 自动重启

主服务和监控服务配置 restart: unless-stopped。它只能说明 Docker 会尝试重启容器，不能证明数据库连接、未完成请求、配置分发或账本能够正确恢复。本次未停止容器做恢复演练。

## 十、监控、日志和故障排查

### 10.1 监控数据流

~~~mermaid
flowchart LR
    H[Higress Envoy\n15000]
    HA[Higress Agent\n15020]
    P[Prometheus\n15 秒抓取，7 天]
    G[Grafana\n12 面板]
    U[运维浏览器\n127.0.0.1:3000]
    S[(LiteLLM PostgreSQL\nSpendLogs)]

    H -->|HTTP scrape| P
    HA -->|HTTP scrape| P
    P -->|PromQL| G
    U -->|HTTP| G
    S -.当前未接入统一 Dashboard.-> G
~~~

Prometheus 配置在 config/monitoring/prometheus.yml:1-13。Grafana 数据源在 config/monitoring/provisioning/datasources/prometheus.yml:1-9。当前看板主要展示 Envoy QPS、状态码、5xx、上游速率、超时、重试、平均耗时和活跃请求，不能等同于 LiteLLM Token/费用看板。

### 10.2 日志和 Request ID

Higress 产生 Envoy 访问、上游连接和 WASM 日志；LiteLLM 产生 Provider、错误和 SpendLogs。测试器发送 X-Request-ID，并尝试读取 X-Request-ID 或 X-LiteLLM-Call-ID，代码在 test/test_gateway_resilience.py:110。当前未确认该 ID 是否在两侧始终保持一致，也未发现统一日志查询页面。

### 10.3 故障排查表

| 现象 | 排查位置 | 典型原因 | 处理方式 |
| --- | --- | --- | --- |
| 8080 连接失败 | Docker、Higress、Envoy /ready | 容器未运行、端口冲突、未 ready | ps、inspect、/ready |
| 4000 liveness 失败 | LiteLLM 日志、挂载、DB | 配置解析、补丁、数据库或 migration | 查日志、检查容器内配置 |
| 401 无 Key | Key Auth/LiteLLM auth | 缺 Header、Key 无效、路径不一致 | 核对 Host、入口和 Key |
| 403 无模型权限 | LiteLLM Key models | Key 没授权；专用路径固定主密钥 | 核对下游身份和模型权限 |
| 404 路由失败 | Ingress/McpBridge/Host | Host 不匹配、runtime 未生效 | 查 Envoy 生效配置 |
| 400 参数失败 | LiteLLM 转换器/Provider | 输入格式、drop_params、Responses 限制 | 查 request_id 和错误体 |
| 429 | Higress/LiteLLM/Provider | 入口限流、业务限流、上游配额 | 对照两侧日志，确认来源 |
| 5xx | LiteLLM/Provider/Higress | Provider 错误、连接失败、配置错误 | 分层定位，检查重试放大 |
| 200 无正文 | LiteLLM/Provider/测试器 | 空 choices、输出预算、异常包装 200 | 检查 choices、finish_reason、usage |
| SSE 无 DONE | 客户端/Higress/LiteLLM/Provider | 断流、超时、缓冲或 Provider 中断 | 查 Content-Type、事件和连接日志 |
| 看板无数据 | Prometheus targets/查询 | 无流量、采样窗口、标签筛选 | 查 targets 和原始 PromQL |
| 费用为 0 | SpendLogs/价格表/Provider usage | 价格缺失、未知或未入账 | 对照 usage、价格版本和账单 |
| DB 无记录 | LiteLLM/PostgreSQL | 连接、迁移或异步写入失败 | 查只读表和日志 |
| 容器重启 | Docker inspect/日志 | 启动命令、配置、资源不足 | 区分进程错误和 Docker 重启 |
| 配置不生效 | 挂载、控制面、服务进程 | 只改快照、未重载、未重启 | 查容器内文件和 Envoy 配置 |

## 十一、当前架构缺口和风险

| 优先级 | 问题 | 证据 | 影响 | 建议方案 | 验证标准 |
| --- | --- | --- | --- | --- | --- |
| P0 | 专用 AI 路由使用共同主密钥 | runtime/higress/wasmplugins/ai-proxy.internal.yaml:55；只读比对确认相等 | 消费者无法独立授权、限额和审计 | 使用受限 Virtual Key 或可信映射，主密钥仅管理用途 | 两身份下游和 SpendLogs 可区分，越权失败 |
| P0 | 敏感值硬编码，4000/8001/8080/8443 宽绑定 | docker-compose.yml:9、35、56 | 泄露、入口绕过和管理面暴露 | 秘密管理、轮换、内网绑定和防火墙 | Git 无秘密，外部不能直达内核和管理端口 |
| P0 | 根仓库无提交，runtime 被忽略，gitlink 无 .gitmodules | Git status；.gitignore:1 | 无法审计、复建和回滚 | 建立脱敏基线，固定镜像/源码摘要和 runtime 生成方式 | 干净目录可复建最小链路 |
| P0 | 运行 LiteLLM 1.100.0，源码标记 1.102.0 | sources/litellm-src/pyproject.toml:3 | 排障和补丁验证失真 | 绑定镜像 digest、源码 commit、补丁摘要 | 三者一一对应 |
| P1 | 校园 SSO、组织、应用权限未接入 | User/Team 数据和 PRD F03 | 不能多用户上线 | 接入学校身份和组织同步 | 双组织、离职、转组和越权用例通过 |
| P1 | 费用记录全为 0，价格和账单口径未定 | SpendLogs schema:632；只读快照 | 无法对账和做预算承诺 | 固定价格版本、费用分类和失败重试规则 | usage、账本和余额可核对 |
| P1 | RPM/TPM/并发/周期额度未配置 | sources/litellm-src/schema.prisma:435；DB 统计字段为 0 | 无公平性和预算边界 | 选定唯一权威和共享状态 | 竞争、恢复、周期和重复结算通过 |
| P1 | Fallback、多 deployment、会话粘性未形成 | 单 Provider，failover=false | 故障时无业务级接管 | 配置兼容模型池和重试边界 | 故障注入切换受控，断流不拼接 |
| P1 | Higress 重试和 LiteLLM 重试边界未统一 | runtime/higress/ingresses/ai-route-litellm-ai-test.internal.yaml:7-11 | 请求放大和非幂等重放 | 指定唯一业务重试层 | attempt 数、总时限和幂等可证明 |
| P1 | 模型能力和别名有疑点 | config/litellm.yaml:16 | 用户选择和参数语义错误 | 审核别名、上下文、能力和价格 | 目录与实际能力矩阵一致 |
| P1 | 业务监控、告警、统一审计缺失 | Dashboard 12 面板；rule_groups=0；AuditLog=0 | 无法运营和追责 | 关联 request_id/attempt_id，补告警和留存 | 触发、恢复、导出和留存可验证 |
| P1 | 管理后台和模型广场未闭环 | 未发现项目门户；PRD F08 | 用户不能自助接入 | 复用 LiteLLM API，建设统一门户 | 用户走通登录、选模、建 Key、调用、查消耗 |
| P1 | HTTPS/CORS/安全头待验证 | 8443 仅端口声明 | 浏览器和公网策略不明确 | TLS、CORS、安全头和边界评审 | 握手、预检和扫描通过 |
| P1 | 两个测试漏检已复现 | test_dual_gateway.py；mock.test.mjs | 假阳性影响验收 | 修正断言和输入集合 | 空对象/非法模型可靠失败 |
| P2 | 镜像使用 latest/main-stable | docker-compose.yml:51；docker-compose.benchmark.yml:34 | 重启行为不可比 | 固定 digest 和 SBOM | 同配置复建同版本 |
| P2 | Grafana 匿名 Viewer | docker-compose.monitoring.yml:23-27 | 监控数据暴露 | 生产改为 SSO/RBAC | 未授权访问失败 |
| P2 | 压测配置与生产配置差异大 | docker-compose.benchmark.yml:33；doc/gateway-capacity-benchmark.md:17 | 不能证明生产容量 | 用正式配置隔离压测 | 输出带版本和治理配置的容量报告 |
| P2 | 文档历史状态漂移 | doc/higress-monitoring-local.md:52 | 接手人误判当前状态 | 标记日期和版本 | 文档与运行证据可追溯 |

## 十二、后续完整建设路线

未取得明确开工日、截止日和人员配置，以下使用相对工作日。估算假设：2 名后端/网关开发、1 名前端、1 名测试、产品和运维各至少 0.5 人持续投入；身份、模型凭证、测试环境和部署授权按阶段提供。1 人日表示 1 人投入 1 个工作日，不等于日历工期。

| 阶段 | 任务 | 负责组件 | 前置条件 | 预估工作量 | 交付物 | 验收标准 |
| --- | --- | --- | --- | --- | --- | --- |
| 1. 架构配置收敛 | 固定版本；确认身份/额度权威；确定首批模型、客户端和 SLO；补无秘密环境模板 | Git、Compose、LiteLLM、Higress | 指定负责人 | 4-6 人日，2-3 工作日 | 版本矩阵、决策记录、范围清单 | 唯一入口、身份链、版本和 P0 负责人明确 |
| 2. 可复建最小链路 | 整理 Compose、外部卷和 Higress 初始化；补健康检查；修复两个测试漏检 | Compose、runtime、test | 阶段 1 | 8-12 人日，4-6 工作日 | 干净环境启动包和回归测试 | 空环境启动，/ready、liveness、受限 /v1/models 通过 |
| 3. 可信身份链 | 消除主密钥代用；设计消费者 Key 到 Virtual Key/User 的映射；实现轮换/撤销 | Key Auth、AI Proxy、LiteLLM | 身份决策 | 8-14 人日，5-8 工作日 | 身份配置和隔离测试 | 两身份在下游和账本可区分，越权和撤销正确 |
| 4. 校园认证权限 | SSO/OIDC/LDAP；用户、组织、应用、角色同步；模型和 Key 权限交集 | 管理服务、LiteLLM API、门户 | 学校协议和组织样本 | 16-24 人日，8-12 工作日，可并行 | 同步任务、权限矩阵、接口/页面 | 登录、授权、离职、转组和越权通过 |
| 5. 模型路由治理 | 审核 8 个别名；能力矩阵、价格、多 deployment、Fallback、重试和会话策略 | LiteLLM、Higress | 备用资源和费用规则 | 14-22 人日，7-11 工作日 | 模型目录、路由和健康配置 | 故障切换受控，不兼容模型不进入候选 |
| 6. 限流预算计量 | 选择 LiteLLM 业务额度权威；RPM/TPM/并发/周期；Redis 或替代共享状态；usage/spend 对账 | LiteLLM、PostgreSQL、共享状态 | 身份和费用口径 | 18-28 人日，9-14 工作日 | 账本、配额策略、告警阈值 | 预占、取消、跨周期、重试和重复事件幂等 |
| 7. 日志指标审计 | 统一 request_id/attempt_id；业务看板、告警、留存和脱敏 | Higress、LiteLLM、Prometheus/Grafana | 数据契约 | 12-18 人日，6-9 工作日，可并行 | 统一看板、告警、审计查询 | 按身份/模型/渠道下钻，告警触发/恢复可复现 |
| 8. 门户和接入支持 | 模型广场、Key 生命周期、额度查询、示例、公告、FAQ、客户端矩阵 | 前端、管理 API、文档 | 权限和计量接口稳定 | 20-30 人日，10-15 工作日 | 门户、教程、兼容性清单 | 用户完成登录、选模、建 Key、调用、查消耗 |
| 9. 流式自动化安全测试 | 真实 Responses/流式、断流、取消、超时、Fallback、越权、TLS/CORS、安全回归 | test、补丁、Higress | 版本和测试 Key 固定 | 14-22 人日，7-11 工作日 | 测试报告、缺陷清单 | P0/P1 缺陷清零，流式结束和断流用例通过 |
| 10. 部署交付 | 固定 digest；备份恢复、配置回滚、密钥轮换、节点/Provider 故障演练；值班手册 | Compose、PostgreSQL、监控 | 前阶段完成和部署授权 | 12-18 人日，6-9 工作日 | 发布包、回滚手册、交接清单 | 干净环境复建，恢复和回滚达标 |
| 11. 试点上线 | 选组织/应用灰度，观察成功率、延迟、费用、告警、日志，处理遗留问题 | 全链路 | P0 门禁通过 | 5-8 人日，至少 3 工作日观察 | 试点报告和上线决策 | 观察窗口无严重事故，关键指标符合确认口径 |

可并行：版本基线、门户静态原型、测试器修复、监控查询设计。必须串行：身份决策在权限和计量之前；可复建链路在真实联调之前；费用口径在预算验收之前；P0 门禁在试点上线之前。

## 十三、最终结论

### 13.1 当前阶段

项目处于本地网关集成和专项验证阶段。已有可运行的 Higress、LiteLLM、PostgreSQL 和监控容器，已有模型别名、路由快照、Responses 补丁和测试工具。尚未成为经过权限、计量、故障恢复和交付复建验收的完整校园 AI 平台。

### 13.2 Higress 已经实际做了什么

Higress 已实际承担 8080/8443 入口、Host/Path 路由、McpBridge 到 LiteLLM 的 HTTP 转发、专用路由 Key Auth、AI Proxy 的部分请求头处理，以及 Envoy 指标采集。没有可靠证据证明它已经完成校园 SSO、业务预算、成本账本或完整模型 Fallback。

### 13.3 LiteLLM 已经实际做了什么

LiteLLM 已实际承担 8 个逻辑模型别名、Provider 端点配置、OpenAI 兼容代理底座、部分 Key/User/Team/SpendLogs 数据、Responses callback 和补丁流处理，以及模型目录认证。上游源码中的 Virtual Key、预算、限流、Fallback、团队和管理 API，不能直接当作本项目全部已完成。

### 13.4 当前最完整的可用链路

客户端 -> Higress 8080 -> ai-test.local Ingress -> Key Auth -> AI Proxy -> McpBridge -> LiteLLM 4000 -> 已配置模型 Provider。模型目录和认证已实际验证；真实 Chat、流式、费用、权限和故障恢复仍需受控复验。

### 13.5 当前最关键的三个问题

1. 专用路由用共同主密钥代表消费者，破坏下游独立身份、权限、预算和审计
2. 根仓库无提交，runtime 被忽略，源码与运行镜像版本不一致，无法可靠复建和回滚
3. 费用、额度、限流、Fallback、统一审计和门户没有形成可验证闭环

### 13.6 下一步五件事

1. 确定唯一身份链，把消费者身份安全传到 LiteLLM
2. 建立无秘密、可复建、版本绑定的工程基线
3. 确认价格、预算、限流和失败重试成本口径并做对账
4. 修复两个已复现测试漏检，建立可信异常和流式回归
5. 在以上基础上实施校园 SSO、门户、Fallback、监控审计和正式上线测试

### 13.7 三分钟团队说明

我们现在有一条本地可运行的 AI 网关骨架。客户端先访问 Higress，Higress 负责入口、Host/Path 路由和部分认证，再把请求转给 LiteLLM。LiteLLM 负责逻辑模型到 Provider 的映射、OpenAI 兼容接口和部分调用记录。Prometheus/Grafana 当前主要观察 Higress 的 Envoy 指标。

已验证的是服务进程、模型目录、基础认证、路由可达、Responses 流补丁和基础监控。历史上有部分 Chat、SSE、向量和 OCR 成功记录，但这不等于当前版本完成完整验收。

项目离校园生产平台还缺三类工作：第一，真实的多用户身份传递，不能用共同主密钥；第二，额度、费用、限流、Fallback、审计和告警闭环；第三，可复建、可备份、可回滚的正式交付。下一步应该先固定版本和身份链，打通一条可审计的核心调用，再扩大模型和用户范围。


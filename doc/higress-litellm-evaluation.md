# Higress + LiteLLM API 网关组合评估

## 结论

这个组合总体合理，建议采用，但要把两者的职责固定下来：

- **Higress 放在最外层**，负责域名、TLS、WAF、IP/来源控制、统一入口、基础认证、连接和请求级流量治理。
- **LiteLLM 放在 AI 路由层**，负责 OpenAI/Anthropic/Gemini 等协议适配、模型目录和别名、多供应商路由、重试/Fallback、虚拟 Key、Token/费用统计和 AI 请求日志。
- **自研平台服务放在管理面**，负责校园统一身份认证、用户/组织同步、订阅或额度申请审批、模型目录展示、Key 生命周期编排、公告和帮助文档。
- **Prometheus/Grafana/Loki 或 OpenTelemetry** 作为统一观测后端。Higress 图中的网络指标与 LiteLLM 图中的 Token/费用指标应通过 request ID、user/team/project/model 等维度关联。

图片表达的目标是“一个地址 + 一个密钥，统一调用多家模型”，并包含六组能力：模型与渠道管理、用户与令牌管理、配额与精细计量、数据看板与运行监控、日志审计与安全管控、模型广场与服务支持。前五组可以由两个网关加一套观测系统拼成，第六组和校园业务流程需要管理面补齐。

## 图片内容与产品映射

| 图片需求 | Higress | LiteLLM | 仍需补充 |
|---|---|---|---|
| 统一 Base URL、TLS、域名、IP 控制 | 强 | 可做，但不应作为边缘入口 | 否 |
| OpenAI 兼容入口、协议转换 | AI Proxy 可做 | 强项，覆盖多供应商和多端点 | 对 Claude/Gemini CLI 做版本兼容测试 |
| 模型目录、分组、别名、供应商渠道 | 可配置 AI 路由 | 强项，model list、alias、router | 管理面工作流 |
| 负载均衡、重试、故障切换、模型 Fallback | 可做入口级治理 | 强项，按模型组/供应商做路由和 Fallback | 明确降级策略和用户可见性 |
| API Key、用户、团队、项目、预算 | Consumer/key-auth 等入口认证 | Virtual Key、user/team/project、预算和费率统计 | 校园统一身份、审批、同步 |
| RPM/TPM/并发/Token 限流 | AI Token Rate Limit 插件支持 Redis | Key/team/model 级 RPM/TPM 等 | 统一限流归属，避免两层重复扣减 |
| Token、成本、模型、Key 维度统计 | 网络 QPS、延迟、成功率、流量 | Token、成本、请求明细和模型维度 | 账单口径、价格表审核、数据归档 |
| 请求日志、操作审计、内容审计 | 入口访问日志和插件审计 | 请求/响应日志；管理审计和 SSO 等部分属于 Enterprise | 脱敏、留存、合规策略 |
| 模型广场、文档、FAQ、公告 | 无完整产品能力 | AI Hub/模型展示能力，但完整门户需评估版本/授权 | 自研门户最稳妥 |
| MCP/A2A 等工具服务 | Higress 有 MCP 托管能力 | LiteLLM 也有 MCP/A2A 能力 | 首期建议先收敛 LLM API 范围 |

## 推荐拓扑

```text
客户端：校内系统 / AI Agent / Claude Code / Gemini CLI / OpenCode / SDK
                 |
        DNS + HTTPS + WAF + SSO/OIDC
                 |
       Higress（公网/校内统一入口）
       - 路由、TLS、IP/来源策略、基础限流
       - request-id、访问日志、SSE/长连接保护
                 |
       LiteLLM Proxy（内网 AI 路由层）
       - OpenAI 兼容 API 和必要的 Anthropic 入口
       - model alias、供应商路由、重试、Fallback
       - virtual key、用户/团队/项目预算、Token/成本日志
          |              |                 |
      Redis           PostgreSQL       供应商/私有模型
   限流/缓存/状态       Key/团队/费用       OpenAI、Anthropic、Gemini、Ollama/vLLM...
                 |
      Prometheus + Grafana + Loki/OTel + 对象存储归档
                 ^
      自研管理面：校园 SSO、申请审批、目录/价格、额度、报表、公告
```

### 请求链路建议

1. Higress 校验域名、TLS、来源网络和外层身份，生成或透传 `X-Request-ID`。
2. 管理面给用户/应用分配 LiteLLM virtual key，Key 只授予允许的模型组和额度。
3. Higress 将请求转发到 LiteLLM。对 SSE、长上下文和大响应设置足够的 idle timeout，并关闭会破坏流式响应的缓冲行为。
4. LiteLLM 做模型别名解析、Provider 选择、重试/Fallback，并记录 token、模型、Key、team/project 和估算成本。
5. 两侧日志都写入同一 `request_id`，下游只保留必要的提示词/响应内容，并默认脱敏。

## 关键边界与风险

### 不要让两层同时承担同一种 Token 限流

Higress 的 AI Token Rate Limit 插件适合做入口保护和按 IP、Consumer、Header 或路由的硬上限，基于 Redis；LiteLLM 适合按 virtual key、用户、团队、项目和模型做 RPM/TPM/预算控制。推荐：

- Higress：IP/来源/全局防突发，保护网关和 LiteLLM。
- LiteLLM：用户/应用/团队/模型的业务配额和计费口径。

如果两边都按 token 扣减，流式请求、重试和 Fallback 可能重复计数，用户会遇到“入口放行但后端拒绝”或反过来的情况。必须在设计阶段决定唯一的业务额度权威，一般选 LiteLLM。

### 订阅/OAuth 账号不能直接等同于平台供应商密钥

图中“订阅/OAuth 账号”有两种完全不同的含义：

- **用户自带凭证**：例如 Claude Code Max 的 OAuth，LiteLLM 有透传用户 `Authorization` 的用法；此时平台 Key 负责平台侧授权和计量，OAuth 负责供应商侧身份。
- **学校集中购买的订阅账号**：不能简单把一个账号共享给全校用户。需要确认供应商条款、并发/速率限制、凭证保管和责任归属。工程上更适合使用正式的组织 API/云平台账号，或在管理面实现明确的 BYOK/账号绑定策略。

### LiteLLM 社区版与企业版边界

LiteLLM OSS 已覆盖 OpenAI 兼容网关、virtual key、用户/团队、预算、Fallback、请求日志和基础指标。其文档将 SSO/SCIM、组织级角色、管理操作审计、细粒度企业治理、多区域等列为 Enterprise 能力；采购前应按实际版本和许可证逐项核实，不能把截图中的所有后台功能直接视为免费能力。

### 两个控制台不应都作为最终用户门户

Higress 控制台适合网关和路由运维，LiteLLM 控制台适合 AI 路由、Key 和费用运维。面向教师、学生和业务系统的“模型广场、申请审批、额度、公告、FAQ、调用示例”建议做统一门户，通过 API 编排两个后台，避免用户需要理解两套概念。

### 供应商价格和成本统计需要治理

LiteLLM 按模型价格表估算成本，实际账单可能受缓存、批处理、输入输出 token、订阅计费方式和汇率影响。正式报表应保存价格表版本、原始 token、供应商账单标识和人工修正记录；订阅型账号不要把估算 token 成本直接当成财务账单。

### Fallback 要限制模型访问范围

Fallback 可能把原本只允许访问某模型的 Key 请求转到另一个模型。应开启“Fallback 目标也做访问控制”的策略，给每个模型组定义降级等级，并在响应头和日志里记录原始模型与实际模型，便于解释结果和计费。

## 建议的第一阶段范围

先做一个可运营的 LLM API 平台，不要一开始同时纳入 MCP、A2A、复杂订阅账号和所有客户端。

1. Higress + LiteLLM + Redis + PostgreSQL 的高可用部署，内网跑 LiteLLM，Higress 暴露统一 HTTPS 地址。
2. 先支持 OpenAI Chat Completions、Embeddings 和流式 SSE；对 Claude Code、Gemini CLI、OpenCode 分别做兼容性回归。
3. 管理面接入校园 OIDC/LDAP，建立用户、部门、应用、项目四级关系；调用凭证用 LiteLLM virtual key，原始 Provider key 放 Secret Manager。
4. 实现模型目录/别名、供应商健康检查、同模型多部署、明确的 429/5xx/超时 Fallback。
5. 实现用户/应用的 RPM、TPM、并发和月度额度；业务额度由 LiteLLM 统一计算，Higress 只做外围保护。
6. 打通 Prometheus/Grafana 和结构化日志，以 `request_id` 关联 Higress 和 LiteLLM；默认不记录完整提示词，内容审计单独授权。
7. 发布统一门户：模型说明、价格/额度说明、Key 创建/吊销、调用示例和公告。管理端角色至少区分平台管理员、部门管理员、应用管理员和普通用户。

## 选型判断

**适合采用 Higress + LiteLLM 的情况**：已经有 Kubernetes/网关运维体系，既需要通用 API 网关能力，又需要多模型适配、Fallback、Token/费用计量，并且愿意维护一个管理面。

**只用 Higress 更合适的情况**：模型供应商很少，主要诉求是入口治理和网络观测，希望减少 Python 服务、数据库和 Redis 依赖。

**只用 LiteLLM 更合适的情况**：内部网络简单，暂时没有 WAF、复杂域名路由和微服务入口治理需求，团队更关心模型路由和额度。

对于图片所示的校园平台，推荐组合方案，但要把 LiteLLM 定位成“AI 模型路由与计量内核”，把 Higress 定位成“统一 API 入口与安全流量层”，再补一套轻量管理面。这比把两个产品串成两个都负责认证、限流、路由和看板的“叠加网关”更容易稳定运行。

## 参考资料

- Higress 项目说明与 AI Gateway 能力：<https://github.com/higress-group/higress>
- Higress AI Token 限流插件：<https://github.com/higress-group/higress/tree/main/plugins/wasm-go/extensions/ai-token-ratelimit>
- LiteLLM 项目与支持的统一 API：<https://github.com/BerriAI/litellm>
- LiteLLM Virtual Keys：<https://docs.litellm.ai/docs/proxy/virtual_keys>
- LiteLLM Production Best Practices：<https://docs.litellm.ai/docs/proxy/prod>
- LiteLLM Enterprise 能力边界：<https://docs.litellm.ai/docs/enterprise>
- LiteLLM Claude Code Max OAuth 透传示例：<https://docs.litellm.ai/docs/tutorials/claude_code_max_subscription>
- LiteLLM Gemini CLI 集成示例：<https://docs.litellm.ai/docs/tutorials/litellm_gemini_cli>

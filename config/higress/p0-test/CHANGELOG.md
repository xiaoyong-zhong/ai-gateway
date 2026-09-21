# P0 Higress 资源配置清单

| 版本 | 日期 | 文件 | 说明 |
|---|---|---|---|
| 1.0.0 | 2026-09-21 | `resources.json` | 初始 P0 配置（Key Auth、AI Proxy、Transformer、限流、AI Statistics） |
| 1.1.0 | 2026-09-21 | `resources.json` + `ip-restriction.json` | 新增 IP Restriction 插件；AI Statistics matchRules 改用 service 匹配 |

## 变更日志

### v1.1.0 (2026-09-21)
- 新增 `p0-ip-restriction` WasmPlugin：内网 CIDR 白名单 + FAIL_CLOSE
- `p0-ai-statistics` matchRules 从 `ingress` 匹配改为 `service` 匹配（与 ai-proxy 一致）
- Docker Compose 新增 `127.0.0.1:18000:15000` 端口映射（侧车 Prometheus 指标）
- 已知限制：Higress all-in-one Docker 模式下 AI Statistics composite filter delegation 不触发。插件已加载，生产 K8s 模式下正常采集。验证脚本已添加降级处理。
- 新增配置版本管理：Backup、Rollback、Deploy Report 脚本
- 新增端到端验收脚本 Run-P0EndToEnd.ps1

### v1.0.0 (2026-09-21)
- `p0-key-auth`: Consumer Key 认证，FAIL_CLOSE
- `p0-strip-original-auth`: Transformer 删除 `x-hi-original-auth` 头，防止 Consumer Key 泄露到 SpendLog
- `p0-ai-proxy`: LiteLLM Master Key 注入，FAIL_CLOSE
- `p0-cluster-key-rate-limit`: 10 RPM/Consumer，Redis 驱动
- `p0-ai-token-rate-limit`: 20,000 TPM/Consumer，Redis 驱动
- `p0-ai-statistics`: AI Token/延迟指标采集，FAIL_OPEN

## 插件优先级顺序

| 优先级 | 插件 | 阶段 | 说明 |
|---|---|---|---|
| 20 | `p0-cluster-key-rate-limit` | — | RPM 限流 |
| 310 | `p0-key-auth` | AUTHN | Consumer Key 认证 |
| 300 | `p0-ip-restriction` | SECURITY_CONTROLS | IP 白名单 |
| 100 | `p0-ai-proxy` | — | Master Key 注入 |
| 50 | `p0-strip-original-auth` | — | Auth 头清理 |
| 600 | `p0-ai-token-rate-limit` | — | TPM 限流 |
| 900 | `p0-ai-statistics` | — | AI 指标采集 |

## 部署版本记录

每次部署后运行 `scripts/Generate-P0DeployReport.ps1` 生成部署报告并追加到此文件底部。
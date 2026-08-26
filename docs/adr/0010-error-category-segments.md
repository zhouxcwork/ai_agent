# 错误码分段：段.码 命名与按段重试策略

---
status: accepted
---

对外错误码从 16 个扁平字符串改为**点分前缀分段**：`<段>.<码>`（如 `route.no_healthy_route`、`output.invalid_json`）。相同类型的错误归入同一段，段决定重试语义，调用方可据段前缀编写退避与告警逻辑，不必逐码记忆。供应商 SDK 异常在 provider 适配层全量翻译为内部异常（瞬态 → RetryableUpstreamError；401/403/404 → UpstreamRejectedError；拒答 → ContentRefusedError），service 层不感知 openai。

## 段 → 重试策略矩阵

| 段 | 同目标重试 | 换候选 Fallback | 计入熔断 | 调用方动作 |
|---|---|---|---|---|
| auth.*（身份） | 否 | 否 | 否 | 修正凭据 |
| request.*（参数/会话引用） | 否 | 否 | 否 | 修正请求 |
| prompt.*（模板资产） | 否 | 否 | 否 | 修正变量/版本 |
| route.unknown_mode | 否 | 否 | 否 | 换档位名 |
| route.no_healthy_route / model_unavailable | 否 | — | —（结果状态） | 稍后重试或改配置 |
| resource.*（资源边界，429） | 网关侧否 | 否 | 否 | 退避后重试 || upstream 瞬态（连接/超时/429/5xx） | 是（attempts_per_target） | 是 | 是 | 无需动作 |
| upstream 拒绝（401/403/404） | 否 | 是 | 是 | 无需动作 |
| stream.*（已流式输出后中断） | 否（避免文本重复） | 否 | — | 重新发请求 |
| output.*（JSON/Schema 校验） | 修复链（无损提取 + 同目标上游重试 max_retries，ADR 0009） | 否 | 否（内容质量非目标健康） | 修 Schema 或提示词 |
| content 拒答（content_filter） | 否 | **否（禁止换模型绕过安全策略）** | 否 | 修改输入内容 |
| platform.misconfigured | 否 | 否 | — | 联系网关管理员 |

## 段集合（10 段）

`auth`（auth.invalid_key / auth.unauthorized）、`request`（validation_error / unsupported_parameter / response_not_found）、`prompt`（unknown_template / missing_variable / render_failed / version_conflict）、`route`（unknown_mode / no_healthy_route / model_unavailable）、`resource`（tenant_rate_limited / tenant_busy / target_busy）、`upstream`（stream_failed，主要出现在流内事件与 trace）、`stream`（client_cancelled，trace 维度）、`output`（invalid_json / schema_validation_failed）、`content`（refused，trace 维度；对外按 OpenAI 方言以 finish_reason=content_filter 透传）、`platform`（misconfigured）。

## Considered Options

- 点分前缀 `段.码`（选定）：SDK 的 `code` 字段自描述，一段一策；代价是 breaking
- 保持扁平码 + 错误体加 `category` 字段：兼容但两套标识长期并存，调用方要判断两个地方
- 数字分段（1xxx/2xxx…）：机器友好但人不可读，且与 OpenAI 生态的字符串 code 惯例不符

## Consequences

- 对外契约 breaking：所有存量 code 值变化，SDK 侧按 code 断言的调用方与测试需同步
- trace 的 error_code 跟随对外码；status 枚举新增 `refused`（拒答非失败，不污染失败率）
- 内容拒答不换模型绕过是治理红线：fallback 链在拒答处终止，透传 200 + finish_reason=content_filter
- 新增错误码必须先归段再命名，无段之码视为契约违规

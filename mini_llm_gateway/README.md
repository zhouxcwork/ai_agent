# Mini LLM Gateway

OpenAI 协议兼容的 LLM 网关：档位路由（fast/smart）、上游多协议适配（同一 DeepSeek 供应商同时经 Anthropic Messages 与 OpenAI Responses 两种协议接入，ADR 0013）、熔断健康度、Prompt 模板管理（Jinja2 + SQLite）、调用 trace 审计、Docker 一键部署。任意 OpenAI SDK 改一个 base_url 即可接入。

## 架构分层

```
api/        HTTP 路由层：协议方言（chat completions / responses）在此翻译，不含业务逻辑
service/    业务编排层：gateway_service（编排）、model_router（档位路由 + 熔断状态机）、limiter（多级资源边界）、repair / syntax_monitor（结构化输出修复与流中语法监控）
provider/   供应商适配层：三种协议适配器（openai_compatible / anthropic_adapter / responses_adapter），密钥只在这一层出现
repository/ 持久化层：aiosqlite，trace / prompt 模板 / responses 会话三张表
schemas/    Pydantic 契约：内部领域模型 + OpenAI 方言入参出参（openai_compat / responses_compat）
config.py   配置加载：config.yaml → GatewayConfig（providers / modes / circuit_breaker）
```

依赖方向：api → service → provider/repository；**方言只存在于 api 层**，内部领域模型协议无关。

## 快速开始（本地）

```bash
cd mini_llm_gateway
uv sync --extra dev
cp .env.example .env         # 填入 DEEPSEEK_API_KEY / OPENAI_API_KEY / GATEWAY_ADMIN_TOKEN
uv run --env-file .env uvicorn mini_llm_gateway.app:create_app --factory --reload
```

打开 http://127.0.0.1:8000/admin 管理模板；http://127.0.0.1:8000/docs 查看接口文档。

## Docker 一键部署

```bash
cp .env.example .env && docker compose up -d   # SQLite 持久化在 ./data
```

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | Chat Completions 方言（非流式 + chunk 流式） |
| POST | `/v1/responses` | Responses 方言（非流式 + 类型化事件流式 + 会话续接） |
| GET | `/v1/models` | 档位列表 + 各路由目标健康状态（gateway_routes 扩展字段） |
| GET | `/v1/traces?limit=50&offset=0` | 调用 trace（见下方字段清单） |
| GET/POST | `/v1/prompts` | 模板列表 / 创建新版本（写需 `X-Admin-Token`） |
| POST | `/v1/prompts/{name}/{version}/render` | 渲染预览 |
| GET | `/admin` | 模板管理页面 |
| GET | `/healthz` | 健康检查（docker-compose healthcheck 使用，不进 /docs） |

错误统一 OpenAI 格式：`{"error": {"message", "type", "param", "code"}}`；**code 为点分分段码 `<段>.<码>`**（ADR 0010），段决定重试语义：

| 段 | 含义 | 典型码 | 调用方动作 |
|---|---|---|---|
| auth | 调用方/管理面身份 | `auth.invalid_key` 401 | 修正凭据 |
| request | 参数与会话引用 | `request.validation_error` 422、`request.response_not_found` 404 | 修正请求 |
| prompt | 模板资产 | `prompt.missing_variable` 400、`prompt.version_conflict` 409 | 修正变量/版本 |
| route | 档位路由 | `route.unknown_mode` 400、`route.no_healthy_route` / `route.model_unavailable` 503 | 稍后重试/改配置 |
| resource | 资源边界 | `resource.tenant_rate_limited` / `tenant_busy` / `target_busy` 429 | 退避后重试 |
| upstream | 供应商侧故障 | `upstream.stream_failed`（流内事件与 trace） | 无需动作（网关已重试/fallback） |
| stream | 流中传输中断 | `stream.client_cancelled`（trace） | 重新发请求 |
| output | 输出校验 | `output.invalid_json` / `output.schema_validation_failed` 502 | 修 Schema/提示词 |
| content | 内容拒答 | `content.refused`（trace；对外透传 finish_reason） | 修改输入内容 |
| platform | 网关自身 | `platform.misconfigured` 503 | 联系管理员 |

## 认证与资源边界（ADR 0011）

- **调用方认证**：`Authorization: Bearer <apikey>`，config.yaml `auth.api_keys` 白名单维护 apikey → tenantId；白名单为空 = 不启用认证（本地开发）
- **四级资源边界，全部 429 快速拒绝**：租户 QPS 令牌桶（容量 = 突发额度）、租户并发、路由目标 QPS（可选，按模型独立限流）、路由目标并发（`供应商/模型` 粒度，同供应商不同模型互不挤占；占满则跳过该候选，全部占满报 `resource.target_busy`）
- **内容拒答不绕过**：上游 `finish_reason=content_filter` 时不重试、不换模型，200 + finish_reason 透传（Responses 方言为 status=incomplete），trace 记 `refused` 状态

## Trace 可观测字段（ADR 0012）

| 维度 | 字段 |
|---|---|
| 关联 | `request_id`（单次调用 ID）、`run_id`（调用方 `X-Run-Id` 头透传）、`endpoint` |
| 路由 | `requested_model`（档位）、`actual_model`（供应商/模型）、`attempts`；候选链与跳过原因在 `route_decision` 结构化日志（按 request_id 关联） |
| Prompt | `prompt_name` / `prompt_version`（不可变，即内容定位） |
| 用量 | `input_tokens`、`output_tokens`、`cached_tokens`（含于 input）、`reasoning_tokens`（含于 output） |
| 延迟 | `latency_ms`（总）、`ttft_ms`（首个业务 delta，非流式为空）；generation = latency − ttft |
| 结果 | `status`（success/failed/refused/cancelled）、`finish_reason`、`error_code`（分段码） |
| 错误 | `error_code`（稳定分段码，HTTP 状态可由其映射）、`upstream_request_id`（定位上游日志） |
| 成本 | `cost_usd`（cached 按价格表可选 `cached` 键折扣计价，缺省=输入全价） |

**指标与日志分工**：Logs（`llm_call_trace` / `route_decision` 结构化日志）记单次细节，Trace（SQLite 表）供查询与离线聚合（`evals/routing_report.py`）；在线 Metrics 端点暂不提供（单实例 MVP，ADR 0012）。

## 档位路由（model 字段语义）与双协议适配

**model 字段只接受档位名**（`fast` / `smart`，config.yaml 可扩展），传具体模型名（如 `gpt-4o`）返回 400 `unknown_mode`。响应 `model` 回显档位名，实际命中的路由目标在扩展字段 `actual_model`（格式 `供应商/模型`，如 `deepseek-anthropic/deepseek-v4-flash`）。

**协议适配发生在档位内部**（ADR 0013）：路由目标经 `providers.*.protocol` 绑定协议适配器——fast 档 V4-Flash 走 Anthropic Messages 端点（`https://api.deepseek.com/anthropic`）、smart 档 V4-Pro 走 OpenAI Responses 端点，两个连接共用同一 API Key；鉴权头、请求体、返回格式与流事件的差异全部封装在适配器内，对上面两层不可见。

- 档位候选池按**权重轮询**分流；主选失败先在同目标按 `retries_per_target`（默认 2，即初次 + 2 次重试 = 最多 3 次尝试）线性退避重试（0.1s / 0.2s，ADR 0014），再切换剩余健康候选（流式仅首块前可重试/切换）
- 每次选路输出 `route_decision` 结构化日志（选中链 + 跳过目标及原因：熔断中/供应商停用/能力不匹配），与 trace 的 attempts/actual_model 互补
- 每个路由目标独立维护**熔断**：连续失败 ≥ 阈值熔断，冷却期满半开试探，成功恢复（参数在 `circuit_breaker` 段）
- 候选耗尽或无健康候选返回 503（`model_unavailable` / `no_healthy_route`）；流式场景以流内失败事件收尾

## SDK 调用示例

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="replace-with-a-long-random-secret")
# api_key 对应 config.yaml auth.api_keys 白名单中的 key；白名单为空时任意值即可

# 非流式
completion = client.chat.completions.create(model="fast", messages=[...])
print(completion.choices[0].message.content, completion.actual_model)

# 流式（chat.completion.chunk + [DONE]）
for chunk in client.chat.completions.create(model="fast", messages=[...], stream=True):
    print(chunk.choices[0].delta.content or "", end="")

# 模板（非标扩展字段）
client.chat.completions.create(
    model="fast", messages=[...],
    extra_body={"gateway_prompt": {"name": "agent_code_reviewer", "version": "v1",
                                    "variables": {"language": "python", "project_type": "LLM Gateway",
                                                  "focus_areas": "安全", "strictness_level": "P2",
                                                  "source_code": "def add(a, b): return a + b",
                                                  "focus_security": "true", "focus_reliability": "",
                                                  "focus_cost": "", "focus_observability": ""}}},
)

# Responses 方言 + 多轮续接
first = client.responses.create(model="smart", input="第一轮")
second = client.responses.create(model="smart", input="第二轮", previous_response_id=first.id)
```

## 结构化输出：两层保证 + 修复边界 + 流式裁决

- **两层保证**：请求时声明 JSON Schema（第一层：供应商结构化模式，三种 mode——`json_schema` / `json_object` / `tool_use`，Anthropic 协议无 response_format，经工具强制调用输出 JSON，ADR 0013）；返回后本地 Schema 校验（第二层）
- **修复边界（非流式）**：输出被 markdown 包裹或夹杂杂文本时先无损提取修复；仍失败按 `structured_output.max_retries`（默认 2）重试上游；耗尽返回明确错误码，绝不静默；内容类错误不计熔断
- **Structured Streaming**：`stream=true` 与 `response_format` 可组合——流中轻量语法监控（破损立即断流省 token），流尾完整 JSON 解析 + Schema 裁决，失败发流内错误事件
- **流式 usage**：上游开启 include_usage，chat 末尾 usage chunk 与 responses 的 completed 事件携带真实 token，trace 记真实成本
- **取消终态**：客户端断连记独立 `cancelled` trace 状态（`error_code=client_cancelled`），可用性统计不被用户取消污染

## 模板治理规则

- `(name, version)` 唯一且**不可变**：页面"修改"= 创建新版本，历史版本永久保留、可回滚
- 渲染使用 Jinja2 **沙箱环境**（SandboxedEnvironment），页面编辑的模板无法执行任意代码
- 缺变量直接报错（`missing_prompt_variable`）；开关变量传空串表示关闭
- 启动自动 seed `agent_code_reviewer/v1`

## 配置说明（config.yaml）

- `providers.*`：协议端点连接（base_url、api_key_env 环境变量名、protocol 协议适配器、enabled 开关）；同一供应商可平铺多条连接（如 deepseek-anthropic / deepseek-responses）
- `modes.<档位>.targets[]`：候选池（provider、model、weight、结构化能力与模式、单价）
- `circuit_breaker`：failure_threshold / cooldown_seconds
- `retries_per_target`：fallback 切换前每目标重试次数（不含初次，线性退避 0.1s/0.2s，ADR 0014）
- `auth.api_keys`：调用方白名单 apikey → tenantId（为空 = 不启用认证）
- `limits`：租户 QPS/burst/并发（default + tenants 覆盖）与路由目标资源边界（`targets`，键 = `供应商/模型`，max_concurrency 必选、qps 可选）
- `database.path`：SQLite 路径；`admin.token_env`：管理令牌环境变量名

## 测试与路由质量报告

```bash
uv run --extra dev pytest                                  # 含真实 openai SDK 直连集成测试（FakeProvider 驱动）
uv run python evals/routing_report.py --db data/gateway.db # 离线路由质量报告（成功率/fallback 率/各目标表现），--json 输出 JSON
```

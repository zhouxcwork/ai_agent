# Mini LLM Gateway

OpenAI 协议兼容的 LLM 网关：档位路由（fast/smart）、上游多协议适配（同一 DeepSeek 供应商同时经 Anthropic Messages 与 OpenAI Responses 两种协议接入，ADR 0013）、熔断健康度、Prompt 模板管理（Jinja2 + SQLite）、调用 trace 审计、Docker 一键部署。任意 OpenAI SDK 改一个 base_url 即可接入。

## 架构分层

```
mini_llm_gateway/
├── api/                  HTTP 路由层：对外方言在此翻译为内部请求，不含业务逻辑
│   ├── chat_routes.py        POST /v1/chat/completions（非流式 + chunk 流式 SSE）
│   ├── responses_routes.py   POST /v1/responses（类型化事件流 + 会话续接入库）
│   ├── model_routes.py       GET /v1/models（档位列表 + gateway_routes 健康扩展字段）
│   ├── trace_routes.py       GET /v1/traces（调用审计查询）
│   ├── prompt_routes.py      /v1/prompts 列表/创建/渲染 + /admin 管理页
│   └── deps.py               认证依赖（Bearer 白名单 / X-Admin-Token）+ 租户限流入口
├── service/              业务编排层：不 import fastapi、不感知任何供应商 SDK
│   ├── gateway_service.py    编排主线：模板渲染→选路→候选循环（重试/切换）→结构化修复→trace 落库
│   ├── model_router.py       档位路由（权重轮询选主选）+ 熔断状态机 + route_decision 留痕
│   ├── limiter.py            多级资源边界：租户 QPS/并发 + 路由目标 QPS/并发（令牌桶 + 槽位）
│   ├── repair.py             非流式结构化失败的无损提取修复（markdown 包裹/杂文本截取）
│   └── syntax_monitor.py     流中 JSON 语法监控（括号平衡/字符串状态，破损即断流）
├── provider/             供应商适配层：API Key 只在这一层出现（ADR 0013）
│   ├── base.py               Provider 统一契约（complete + stream 信号流）+ 内部异常分类
│   ├── openai_compatible.py  OpenAI Chat Completions 协议
│   ├── anthropic_adapter.py  Anthropic Messages 协议（tool_use 强制结构化）
│   └── responses_adapter.py  OpenAI Responses 协议（text.format 结构化）
├── repository/           持久化层：aiosqlite，三张表
│   ├── trace_repository.py   llm_call_traces（只追加）
│   ├── prompt_repository.py  prompt_templates（版本不可变）+ Jinja2 沙箱渲染
│   ├── response_repository.py stored_responses（会话续接，存展开后输入，O(1) 重建）
│   └── database.py           连接与建表/迁移
├── schemas/              Pydantic 契约
│   ├── llm.py                内部领域模型（LLMRequest/Message/Usage，协议无关）
│   ├── openai_compat.py      Chat Completions 方言出入参
│   ├── responses_compat.py   Responses 方言出入参
│   └── prompt.py / trace.py
├── static/               admin.html（模板管理页）/ playground.html（接口测试台）
├── seeds/                agent_code_reviewer.jinja2（启动 seed 模板）
├── errors.py             GatewayError + 错误分段码 handler（ADR 0010）
└── config.py             config.yaml → GatewayConfig（协议连接/档位/熔断/限流/重试）
```

依赖方向：api → service → provider/repository。**方言只存在于 api 层（对北）与 provider 适配器（对南）**，内部领域模型协议无关。

## 请求生命周期（一次调用的完整链路）

```
客户端  Authorization: Bearer <apikey>（X-Run-Id 可选透传）
  │
  ▼
api 层：方言翻译 → LLMRequest（内部领域模型）
  │    deps：认证（apikey → tenantId，白名单空则不启用）→ 租户 QPS 令牌桶 + 并发槽（超限 429 快速拒绝）
  ▼
gateway_service（编排）
  ├─ 模板渲染：gateway_prompt → prompt_repository（Jinja2 沙箱，缺变量 400）
  ├─ model_router.resolve：权重轮询选主选，过滤熔断中/供应商停用/能力不匹配 → route_decision 日志
  ├─ 候选循环（按健康候选链）：
  │    ├─ limiter.try_acquire_target：目标 QPS 桶 + 并发槽（占满跳过该候选）
  │    ├─ 按 target.protocol 分发协议适配器 → 上游；SDK 异常翻译为内部异常（可重试/拒绝/拒答三类）
  │    ├─ 失败：同目标指数退避重试×retries_per_target（0.2s/0.4s，ADR 0014）→ 耗尽切换下一候选（流式仅首块前）
  │    ├─ 拒答（content_filter）：不重试不切换不计熔断，200 透传，trace 记 refused
  │    └─ 成功：非流式走结构化修复链（repair → 有限次上游重试，ADR 0009）；
  │              流式走语法监控 + 流尾 Schema 裁决（ADR 0008）
  ├─ 成本计算（usage 四分类 × 目标价格表）→ trace 落库（失败降级日志，不影响主请求）
  │
  ▼
api 层按方言组装响应：model 回显档位；actual_model 不透出、仅入 Trace（ADR 0015）
```

## 启动指南

### 方式一：本地 uvicorn（日常开发，热重载）

```bash
cd mini_llm_gateway
uv sync --extra dev
cp .env.example .env         # 填入 DEEPSEEK_API_KEY / OPENAI_API_KEY / GATEWAY_ADMIN_TOKEN
uv run --env-file .env uvicorn mini_llm_gateway.app:create_app --factory --reload
```

打开 http://127.0.0.1:8000/admin 管理模板、http://127.0.0.1:8000/playground 接口测试台、http://127.0.0.1:8000/docs 查看接口文档。

### 方式二：PyCharm Debug（断点调试）

项目根 `main.py` 是启动入口（等价 Java 的 main 启动类）：Run → Edit Configurations → 新建 Python 配置，Script 选 `main.py`，**Working directory 设为 `mini_llm_gateway/`**，点 Debug 即可打断点。`main.py` 自动加载同目录 `.env`，且不带 `--reload`（reload 的子进程会导致断点失效）。

### 方式三：Docker 一键部署

```bash
cd mini_llm_gateway
cp .env.example .env && docker compose up -d --build   # SQLite 持久化在 ./data
docker compose logs -f gateway                          # 跟踪日志；docker compose down 停止
```

密钥安全：Dockerfile 只 COPY 源码（`.env` 被 `.dockerignore` 排除出构建上下文），`.env` 经 compose 的 `env_file` **运行时注入**容器，不进镜像层。国内网络提示：拉取 `python:3.12-slim` 超时时先执行 `docker pull docker.m.daocloud.io/library/python:3.12-slim && docker tag docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim`（pip 源已在 compose 中指向清华镜像）。

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
| GET | `/playground` | 接口测试台（全端点联调：流式/结构化/续接/trace/错误场景断言） |
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

**model 字段只接受档位名**（`fast` / `smart`，config.yaml 可扩展），传具体模型名（如 `gpt-4o`）返回 400 `unknown_mode`；**兼容别名**：传入候选池中的供应商模型名（如 `deepseek-v4-pro`）时自动映射到所属档位（ADR 0007 的扩展，档位仍是主推语义）。响应 `model` 回显档位名；实际命中的路由目标（`供应商/模型`，如 `deepseek-anthropic/deepseek-v4-flash`）**不在响应中透出**（ADR 0015），仅记入 Trace，经 `/v1/traces` 审计可查。

**协议适配发生在档位内部**（ADR 0013）：路由目标经 `providers.*.protocol` 绑定协议适配器——fast 档 V4-Flash 走 Anthropic Messages 端点（`https://api.deepseek.com/anthropic`）、smart 档 V4-Pro 走 OpenAI Responses 端点，两个连接共用同一 API Key；鉴权头、请求体、返回格式与流事件的差异全部封装在适配器内，对上面两层不可见。

- 档位候选池按**权重轮询**分流；主选失败先在同目标按 `retries_per_target`（默认 2，即初次 + 2 次重试 = 最多 3 次尝试）**指数退避**重试（0.2s / 0.4s，ADR 0014），再切换剩余健康候选（流式仅首块前可重试/切换）
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
print(completion.choices[0].message.content, completion.model)

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

## curl 示例

以下变量按需替换：`KEY`=auth.api_keys 中的 apikey，`TOKEN`=GATEWAY_ADMIN_TOKEN 的值。

```bash
# 健康检查与档位列表（含各路由目标熔断状态）
curl -s http://127.0.0.1:8000/healthz
curl -s http://127.0.0.1:8000/v1/models -H "Authorization: Bearer $KEY"

# Chat 非流式（fast 档 → Anthropic 协议；smart 档 → Responses 协议）
curl -s -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "fast", "messages": [{"role": "user", "content": "你好"}]}'

# Chat 流式（SSE，-N 关闭缓冲逐块输出）
curl -s -N -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "fast", "messages": [{"role": "user", "content": "数到3"}], "stream": true}'

# 结构化输出（response_format 声明 Schema，返回 content 即合法 JSON）
curl -s -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "fast", "messages": [{"role": "user", "content": "回答：1+1等于几"}],
       "response_format": {"type": "json_schema", "json_schema": {"name": "answer",
         "schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}}}}'

# 模板调用（非标扩展字段 gateway_prompt，变量缺省会报 400）
curl -s -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "smart", "messages": [{"role": "user", "content": "审查这段代码"}],
       "gateway_prompt": {"name": "agent_code_reviewer", "version": "v1",
         "variables": {"language": "python", "project_type": "demo", "focus_areas": "安全",
                       "strictness_level": "P2", "source_code": "def add(a, b): return a + b",
                       "focus_security": "true", "focus_reliability": "", "focus_cost": "", "focus_observability": ""}}}'

# Responses 方言 + 会话续接（第一轮存库，第二轮带 previous_response_id 引用上下文）
FIRST_ID=$(curl -s -X POST http://127.0.0.1:8000/v1/responses \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "smart", "input": "我叫小明，记住这个名字"}' | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
curl -s -X POST http://127.0.0.1:8000/v1/responses \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d "{\"model\": \"smart\", \"input\": \"我叫什么？\", \"previous_response_id\": \"$FIRST_ID\"}"

# Responses 流式（类型化事件：response.created → output_text.delta → response.completed）
curl -s -N -X POST http://127.0.0.1:8000/v1/responses \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "smart", "input": "hi", "stream": true}'

# 模板管理（列表免鉴权；创建需 X-Admin-Token；(name, version) 不可变，重复创建 409）
curl -s http://127.0.0.1:8000/v1/prompts
curl -s -X POST http://127.0.0.1:8000/v1/prompts \
  -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "my_prompt", "version": "v1", "system_template": "你是 {{ product }} 助手"}'
curl -s -X POST http://127.0.0.1:8000/v1/prompts/my_prompt/v1/render \
  -H "Content-Type: application/json" -d '{"variables": {"product": "MiniGW"}}'

# 调用审计（actual_model/token 分类/TTFT/cost 都在这查；时间戳为 UTC）
curl -s "http://127.0.0.1:8000/v1/traces?limit=5" -H "Authorization: Bearer $KEY"
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
- `retries_per_target`：fallback 切换前每目标重试次数（不含初次，指数退避 0.2s/0.4s，ADR 0014）
- `auth.api_keys`：调用方白名单 apikey → tenantId（为空 = 不启用认证）
- `limits`：租户 QPS/burst/并发（default + tenants 覆盖）与路由目标资源边界（`targets`，键 = `供应商/模型`，max_concurrency 必选、qps 可选）
- `database.path`：SQLite 路径；`admin.token_env`：管理令牌环境变量名

## 测试与验收

```bash
uv run --extra dev pytest                                  # 单测/集成测试（含真实 openai SDK 直连，FakeProvider 驱动）
uv run python scripts/verify_gateway.py                    # 交付验收脚本：17 项功能点 PASS/FAIL（--offline 跳过真实上游）
uv run python evals/routing_report.py --db data/gateway.db # 离线路由质量报告（成功率/fallback 率/各目标表现），--json 输出 JSON
```

验收脚本需网关已启动（默认 http://127.0.0.1:8000，可用 `--base-url/--api-key/--admin-token` 覆盖），覆盖：健康检查、双协议路由（fast→Anthropic / smart→Responses）、流式 SSE、结构化两层保证、模板管理与版本不可变、Responses 会话续接、Trace 字段齐全（含 cached/reasoning/TTFT）、认证与错误分段码断言、**指数退避重试**（1s 超时触发 → 3 次尝试耗尽 503）、**限流 429**（并发爆发触发租户资源边界）。非标扩展字段：`gateway_prompt`（模板）与 `timeout_seconds`（单请求上游超时，缺省 30s、上限 120s，SDK 经 `extra_body` 传入）。

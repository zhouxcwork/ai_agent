# Mini LLM Gateway

OpenAI 协议兼容的 LLM 网关：档位路由（fast/smart）、多供应商（openai/deepseek）权重分流、熔断健康度、Prompt 模板管理（Jinja2 + SQLite）、调用 trace 审计、Docker 一键部署。任意 OpenAI SDK 改一个 base_url 即可接入。

## 架构分层

```
api/        HTTP 路由层：协议方言（chat completions / responses）在此翻译，不含业务逻辑
service/    业务编排层：gateway_service（编排）、model_router（档位路由 + 熔断状态机）
provider/   供应商适配层：OpenAI Compatible（AsyncOpenAI），密钥只在这一层出现
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
| GET | `/v1/traces?limit=50&offset=0` | 调用 trace（含 endpoint / 档位 / 实际目标 / 成本） |
| GET/POST | `/v1/prompts` | 模板列表 / 创建新版本（写需 `X-Admin-Token`） |
| POST | `/v1/prompts/{name}/{version}/render` | 渲染预览 |
| GET | `/admin` | 模板管理页面 |

错误统一 OpenAI 格式：`{"error": {"message", "type", "param", "code"}}`。

## 档位路由（model 字段语义）

**model 字段只接受档位名**（`fast` / `smart`，config.yaml 可扩展），传具体模型名（如 `gpt-4o`）返回 400 `unknown_mode`。响应 `model` 回显档位名，实际命中的路由目标在扩展字段 `actual_model`（格式 `供应商/模型`，如 `deepseek/deepseek-chat`）。

- 档位候选池按**权重轮询**分流；主选失败自动切换剩余健康候选（流式仅首块前可切换）
- 每个路由目标独立维护**熔断**：连续失败 ≥ 阈值熔断，冷却期满半开试探，成功恢复（参数在 `circuit_breaker` 段）

## SDK 调用示例

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="anything")

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

- **两层保证**：请求时声明 JSON Schema（第一层：供应商结构化模式 / schema 注入）；返回后本地 Schema 校验（第二层）
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

- `providers.*`：供应商连接（base_url、api_key_env 环境变量名、enabled 开关）
- `modes.<档位>.targets[]`：候选池（provider、model、weight、结构化能力、单价）
- `circuit_breaker`：failure_threshold / cooldown_seconds
- `database.path`：SQLite 路径；`admin.token_env`：管理令牌环境变量名

## 测试

```bash
uv run --extra dev pytest    # 含真实 openai SDK 直连集成测试（FakeProvider 驱动）
```

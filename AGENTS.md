# homework（极客邦 AI Agent 课程作业仓库）

课程实战作业集合。当前唯一项目是 `mini_llm_gateway/`（分层版 LLM Gateway，迁移自课程 week01/1-6 单文件 gateway.py，参考 1-7 分层版）。

## Agent skills

### Issue tracker

Issues 以 markdown 文件跟踪在仓库 `.scratch/<feature>/` 目录下（本地跟踪器，不用 GitHub Issues）。见 `docs/agents/issue-tracker.md`。

### Triage labels

使用默认五个 triage 标签（needs-triage / needs-info / ready-for-agent / ready-for-human / wontfix）。见 `docs/agents/triage-labels.md`。

### Domain docs

单上下文布局：根级 `CONTEXT.md` + `docs/adr/`。见 `docs/agents/domain.md`。

## 目录

- `mini_llm_gateway/`：独立 Python 项目（自带 pyproject.toml 和 .venv，用 uv 管理）
- `CONTEXT.md`：领域术语表（平台模型/供应商模型/版本/Trace 等概念的权威定义）
- `docs/adr/`：架构决策记录（SQLite 选型、模板版本不可变）
- 根 `pyproject.toml`：PyCharm 生成的空壳，可忽略；真正的工作目录是 `mini_llm_gateway/`

## 常用命令（均在 mini_llm_gateway/ 下执行）

```bash
uv sync --extra dev                                   # 安装依赖（含测试依赖）
uv run --extra dev pytest                             # 全量测试
uv run --extra dev pytest tests/test_gateway.py -k fallback   # 聚焦单测
uv run --env-file .env uvicorn mini_llm_gateway.app:create_app --factory --reload  # 本地启动
docker compose up -d                                  # Docker 一键部署（SQLite 持久化在 ./data）
```

## 架构分层与边界（改动前必读）

```
api/        路由层：OpenAI 方言（chat completions / responses）在此翻译，协议分离边界
service/    编排层：gateway_service（fallback、trace）+ model_router（档位路由、熔断状态机）
provider/   供应商适配：AsyncOpenAI；API Key 只允许在这一层出现
repository/ 持久化：aiosqlite，trace / prompt 模板 / responses 会话三张表
schemas/    Pydantic 契约：内部领域模型 + openai_compat / responses_compat 方言模型
config.py   config.yaml → GatewayConfig
```

- 依赖方向：api → service → provider/repository；service 不 import fastapi、不感知任何 OpenAI 方言与供应商 SDK 异常（openai 异常在 provider 适配层翻译为 RetryableUpstreamError）
- 端点：`POST /v1/chat/completions`（chunk 流式）、`POST /v1/responses`（事件流式 + 会话续接）、`GET /v1/models`（档位 + 健康状态）、`GET /v1/traces`、`GET|POST /v1/prompts`、`GET /admin`
- **档位路由**：model 字段只收档位名（fast/smart），响应 model=档位、actual_model=供应商/模型（ADR 0007）
- **model 字段回显 requested 而非实际目标**是刻意决定（ADR 0004），不要"修复"成 OpenAI 语义
- 测试通过 `create_app(config, provider)` 注入 FakeProvider（见 tests/conftest.py）；openai SDK 经 ASGI transport 直连做集成测试

## 关键约定

- **密钥**：config.yaml 只写环境变量名（`api_key_env`），真实 Key 从 env/.env 注入；`mini_llm_gateway/.env` 已 gitignore，模板是 `.env.example`
- **Prompt 模板版本不可变**：`(name, version)` 唯一，无 UPDATE/DELETE；"修改"= 创建新版本（见 docs/adr/0002）
- **Jinja2 沙箱**：模板渲染必须用 SandboxedEnvironment（防 SSTI），StrictUndefined（缺变量报错）
- **SSE 与结构化输出互斥**：流式入口在返回 StreamingResponse 前先做档位/模板预校验（validate_request，不推路由轮询计数）
- **trace 只追加**：endpoint 列区分来源（chat_completions/responses）；写库失败降级日志不影响主请求
- **会话续接**：stored_responses 存展开后的完整输入，previous_response_id O(1) 重建（ADR 0006）；store=false 不入库
- **管理鉴权**：模板写接口用 `X-Admin-Token` 头，token 环境变量名在 config.yaml `admin.token_env`

## 注意事项

- 修改领域术语或语义前先读 `CONTEXT.md`，术语冲突时以它为准并同步更新
- 配置路径解析顺序：显式参数 > `GATEWAY_CONFIG` 环境变量 > cwd 的 config.yaml
- 课程参考源码在仓库外：`/Users/zhouxincheng/Documents/work/code/study/python/ai-agent-fullstack-training/course_code/week01/`（1-6 单文件版、1-7 分层参考版）
- 冒烟测试可用 `/Users/zhouxincheng/Documents/work/code/.env` 中的 DEEPSEEK_API_KEY（含密码，禁止提交或硬编码）
- 禁止自动提交 git（工作区当前全部为未跟踪文件，提交需用户明确指示）

# homework（极客邦 AI Agent 课程作业仓库）

课程实战作业集合。当前唯一项目是 `mini_llm_gateway/`（分层版 LLM Gateway，迁移自课程 week01/1-6 单文件 gateway.py，参考 1-7 分层版）。

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
api/        路由层：只做协议转换和入口校验，禁止写业务逻辑
service/    编排层：fallback、重试、成本计算、trace 记录
provider/   供应商适配：AsyncOpenAI；API Key 只允许在这一层出现
repository/ 持久化：aiosqlite，trace 表 + prompt 模板表
schemas/    Pydantic 契约：对外协议，改动需评估兼容性
config.py   config.yaml → GatewayConfig
```

- 依赖方向：api → service → provider/repository；service 不 import fastapi
- 测试通过 `create_app(config, provider)` 注入 FakeProvider（见 tests/conftest.py）

## 关键约定

- **密钥**：config.yaml 只写环境变量名（`api_key_env`），真实 Key 从 env/.env 注入；`mini_llm_gateway/.env` 已 gitignore，模板是 `.env.example`
- **Prompt 模板版本不可变**：`(name, version)` 唯一，无 UPDATE/DELETE；"修改"= 创建新版本（见 docs/adr/0002）
- **Jinja2 沙箱**：模板渲染必须用 SandboxedEnvironment（防 SSTI），StrictUndefined（缺变量报错）
- **SSE 与结构化输出互斥**：`stream + response_schema` 在 Pydantic 层拦截；流式入口在返回 StreamingResponse 前先做模型/模板校验
- **trace 与模板不做外键**：trace 只追加不修改；trace 写库失败降级为日志，不影响主请求
- **种子模板**：启动 lifespan 自动 seed `agent_code_reviewer/v1`（包内 `seeds/`），已存在则跳过
- **管理鉴权**：模板写接口用 `X-Admin-Token` 头，token 环境变量名在 config.yaml `admin.token_env`

## 注意事项

- 修改领域术语或语义前先读 `CONTEXT.md`，术语冲突时以它为准并同步更新
- 配置路径解析顺序：显式参数 > `GATEWAY_CONFIG` 环境变量 > cwd 的 config.yaml
- 课程参考源码在仓库外：`/Users/zhouxincheng/Documents/work/code/study/python/ai-agent-fullstack-training/course_code/week01/`（1-6 单文件版、1-7 分层参考版）
- 冒烟测试可用 `/Users/zhouxincheng/Documents/work/code/.env` 中的 DEEPSEEK_API_KEY（含密码，禁止提交或硬编码）
- 禁止自动提交 git（工作区当前全部为未跟踪文件，提交需用户明确指示）

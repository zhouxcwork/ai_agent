# Mini LLM Gateway

分层版迷你 LLM Gateway：统一调用协议、Prompt 模板管理（Jinja2 + SQLite）、调用 trace 落库、Docker 一键部署。
迁移自课程 week01/1-6 的单文件 `gateway.py`。

## 架构分层

```
api/        HTTP 路由层：协议转换、入口校验，不含业务逻辑
service/    业务编排层：模板渲染、模型校验、fallback 重试、成本计算、trace 记录
provider/   供应商适配层：OpenAI Compatible（AsyncOpenAI），密钥只在这一层出现
repository/ 持久化层：aiosqlite，trace 表 + prompt 模板表
schemas/    Pydantic 契约：请求/响应/trace/模板
config.py   配置加载：config.yaml → GatewayConfig
```

## 快速开始（本地）

```bash
cd mini_llm_gateway
uv sync --extra dev          # 安装依赖
cp .env.example .env         # 填入 DEEPSEEK_API_KEY 等
uv run --env-file .env uvicorn mini_llm_gateway.app:create_app --factory --reload
```

打开 http://127.0.0.1:8000/admin 管理模板；http://127.0.0.1:8000/docs 查看接口文档。

## Docker 一键部署

```bash
cp .env.example .env   # 填入真实 Key
docker compose up -d   # SQLite 数据持久化在 ./data
```

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/llm` | 非流式调用（支持 response_schema 结构化输出） |
| POST | `/v1/llm/stream` | SSE 流式调用 |
| GET | `/v1/traces?limit=50&offset=0` | 调用 trace（SQLite 持久化） |
| GET | `/v1/prompts` | 模板列表（可按 name 过滤） |
| GET | `/v1/prompts/{name}/{version}` | 模板详情 |
| POST | `/v1/prompts` | 创建模板/新版本（需 `X-Admin-Token` 头） |
| POST | `/v1/prompts/{name}/{version}/render` | 渲染预览 |
| GET | `/admin` | 模板管理页面 |
| GET | `/healthz` | 健康检查 |

## 使用模板调用

启动时自动 seed `agent_code_reviewer/v1`（Jinja2，变量：language、project_type、focus_areas、
strictness_level、source_code，以及 focus_security / focus_reliability / focus_cost /
focus_observability 四个开关——传空串 `""` 表示关闭）。

```bash
curl -N http://127.0.0.1:8000/v1/llm -H 'Content-Type: application/json' -d '{
  "model": "general-primary",
  "prompt": {
    "name": "agent_code_reviewer",
    "version": "v1",
    "variables": {
      "language": "python",
      "project_type": "LLM Gateway",
      "focus_areas": "安全与可靠性",
      "strictness_level": "strict",
      "source_code": "def add(a, b):\n    return a + b",
      "focus_security": "true",
      "focus_reliability": "true",
      "focus_cost": "",
      "focus_observability": ""
    }
  },
  "messages": [{"role": "user", "content": "请审查上面的代码"}]
}'
```

## 模板治理规则

- `(name, version)` 唯一且**不可变**：页面上的"修改"是创建新版本，历史版本永久保留、可回滚
- 渲染使用 Jinja2 **沙箱环境**（SandboxedEnvironment），页面编辑的模板内容无法执行任意代码
- 缺变量直接报错（`missing_prompt_variable`），避免静默产出残缺提示词
- trace 与模板不做外键关联，模板演进不影响历史审计记录

## 配置说明（config.yaml）

- `database.path`：SQLite 文件路径（默认 `data/gateway.db`）
- `models.*`：平台模型名 → 供应商模型、base_url、**api_key_env（环境变量名，非 Key 本身）**、结构化能力、单价
- `fallback_model`：主模型不可用时的备用模型
- `admin.token_env`：模板写接口的管理令牌环境变量名

## 测试

```bash
uv run --extra dev pytest
```

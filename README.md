# 极客时间 AI Agent 训练营 · 课后作业仓库

收录训练营各次课程的实战作业。每个作业一个独立目录，自带完整源码、启动指南、测试与验收脚本，互不共享运行环境。

## 作业索引

| # | 作业 | 课程单元 | 状态 | 入口 |
|---|---|---|---|---|
| 1 | **Mini LLM Gateway**：OpenAI 协议兼容的 LLM 网关——同一 DeepSeek 供应商经 Anthropic Messages / OpenAI Responses 双协议接入（适配器模式），档位路由、SSE 流式、结构化输出、Prompt 版本管理、Token/延迟可观测、指数退避与按模型限流 | week01 | ✅ | [`mini_llm_gateway/`](mini_llm_gateway/README.md) |
| 2 | 待补充 | — | ⏳ | — |

作业一的一键验收（需先启动网关）：

```bash
cd mini_llm_gateway
uv run --extra dev pytest                # 单测 + 集成测试（123 项）
uv run python scripts/verify_gateway.py  # 交付验收：19 项功能点
```

## 仓库约定

- **作业独立自包含**：每个作业目录自带 `pyproject.toml` / README（启动指南、curl 示例、验收方式）与测试，根目录不放任何项目配置
- [`CONTEXT.md`](CONTEXT.md)：领域术语表（档位、路由目标、协议适配器等概念的权威定义），随作业演进持续沉淀
- [`docs/adr/`](docs/adr/)：架构决策记录（15 篇：SQLite 选型、模板版本不可变、双协议适配、退避语义、错误分段等）
- [`AGENTS.md`](AGENTS.md)：面向 AI 协作的仓库说明（目录、命令、约定）

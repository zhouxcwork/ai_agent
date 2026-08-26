# 自有 /v1/llm 协议替换为 OpenAI Chat Completions 兼容协议

---
status: accepted
---

网关最初有自定义协议端点（/v1/llm、/v1/llm/stream，自有请求/响应与 SSE 事件格式）。决定用单一 `/v1/chat/completions` 兼容端点完整替换它们，而不是并存：调用方主要是持有 OpenAI SDK 的 Agent，"已有 SDK 无缝接入"是硬需求，并存两套方言会长期维持双倍协议翻译与测试面。模板机制经非标扩展字段 `gateway_prompt`（SDK `extra_body`）保留，内部领域模型与协议分离边界（API 层）不变。代价：自有协议的 `timeout_seconds` 直传和简洁 SSE 事件格式退场，全部调用方需迁移。

## Considered Options

- 新增并存：零迁移成本，但双协议长期共存、测试面翻倍
- 完整替换（选定）：一种方言，SDK 即客户端

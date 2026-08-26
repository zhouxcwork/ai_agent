# 引入第二种官方方言：Responses 端点与 Chat Completions 并存

---
status: accepted
---

ADR-0003 用单一 Chat Completions 方言替换了自有协议，理由是"一种方言，SDK 即客户端"。本决定扩展它：再引入 `/v1/responses`（OpenAI Responses 方言）作为第二个对外入口。理由：Agent 框架生态两种官方协议都在广泛使用（新框架默认 Responses），只支持一种会把一部分"已有 SDK 无缝接入"的调用方挡在门外。关键约束不变：方言只存在于 API 层，内部仍是单一领域模型（LLMRequest/LLMResponse），service/provider 不感知协议；因此双入口的成本是两套薄翻译层，而非双倍核心逻辑。

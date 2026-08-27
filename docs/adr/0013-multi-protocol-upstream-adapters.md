# 上游多协议适配：供应商与协议解耦

---
status: accepted
---

课程作业的训练目标是"两种不同 API 协议"的适配器封装：DeepSeek v4 pro 走 **OpenAI Responses API**、v4 flash 走 **Anthropic Messages API**。此前上游只有一种协议（OpenAI Chat Completions 兼容，所有目标统一 `chat.completions.create`）；项目的"两种方言"（ADR 0005）是对外入口翻译，方向相反，不能视为达标。

DeepSeek 官方同时提供 Anthropic 兼容端点（`https://api.deepseek.com/anthropic`）与 Responses API，作业组合真实可调用。

## 决策

- **新增两个协议适配器**：`anthropic_adapter`（Anthropic Messages 协议）与 `responses_adapter`（OpenAI Responses 协议），与既有 `openai_compatible` 一样实现统一 `Provider` 契约；鉴权、请求体、返回格式、流事件的翻译全部封在适配器内，`gateway_service` 零改动
- **供应商与协议解耦**：`ProviderConfig` 增加 `protocol` 字段；`providers` 平铺多条连接，`deepseek-anthropic` / `deepseek-responses` 与 `deepseek` 共用同一 `DEEPSEEK_API_KEY`——一个 provider 条目 = 一个协议端点连接
- **保留档位路由**（ADR 0007 不动）：model 字段仍只收档位名，协议适配发生在档位内部的路由目标上（fast → v4-flash/Anthropic，smart → v4-pro/Responses）
- **Anthropic 结构化输出走 tool-use 强制**：Anthropic 协议没有 `response_format`，注册输出 JSON 的工具并 `tool_choice` 强制调用，从 `tool_use` 内容块提取 JSON；`structured_output_mode` 新增枚举值 `tool_use`，与既有 json_schema / json_object 并列
- **SDK 选型**：引入 `anthropic` 官方 SDK（base_url 指向 DeepSeek 端点），与 openai SDK 对称，SSE 事件解析交给 SDK

## 明确不做的

- **model 字段直连模型名**：作业"根据 model 字段路由到适配器"在档位路由内部即成立；为验收兼容而接受模型名会破坏档位语义（ADR 0007）
- **provider 下挂 endpoints[] 列表**：概念上更"正"，但要动 resolver、目标 key、健康展示一整条链；平铺连接的成本只是 provider 名带协议后缀

## Considered Options

- 双协议差异落点：改上游（选定）vs 把对外双方言解释为达标（否：入口翻译不涉及供应商协议封装，验收讲不通）
- Anthropic 结构化：tool-use 强制（选定，协议真实差异所在）vs prompt 注入仅靠第二层校验兜底（第一层保证名存实亡）
- Anthropic HTTP 选型：官方 SDK（选定）vs 裸 httpx 手写 SSE（代码量大、解析易错）

## Consequences

- `actual_model` 形如 `deepseek-anthropic/deepseek-v4-flash`，路由目标 key 粒度与限流/熔断语义不变
- 上游 Responses 端点无状态，与网关会话续接（ADR 0006）不冲突：网关存的就是展开后的完整输入
- Anthropic 流事件（message_start / content_block_delta / message_delta）与 Responses 流事件均翻译为统一信号流（UpstreamID / str 增量 / FinishReason / Usage）
- Anthropic usage 映射：`cache_read_input_tokens` → `cached_tokens`；reasoning 细分缺失记 0（ADR 0012 防御性提取原则）
- 新增依赖 `anthropic`；适配器单测以 SDK mock 驱动，真实路径靠 DeepSeek 冒烟验证

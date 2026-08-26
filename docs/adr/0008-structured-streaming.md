# 解除流式与结构化输出的互斥（Structured Streaming）

---
status: accepted
---

首版 spec 明确决策"stream 与 response_format 互斥（400）"，理由是部分 JSON 无法做 Schema 校验、避免调用方误以为流中就能得到校验保证。本轮将其推翻：调用方对长结构化输出有真实的流式体验需求，而互斥迫使他们退回非流式。替代方案是分层验证：**手写轻量语法监控器**（括号平衡 + 字符串转义状态机，零新依赖）在流中逐 delta 推进，发现语法破损立即断流报错（省 token、快速失败）；完整 JSON 解析与 Schema 校验统一在**流结束后**执行（部分 JSON 本就无法验 Schema，强行流中验是伪保证）。流式结构化失败不做修复重试（文本已发出，重试会导致重复），修复+重试仅存在于非流式路径（见 ADR 0009 的输出修复边界）。

## Consequences

- 调用方拿到流式 JSON 增量的同时，只在流尾获得"是否通过 Schema"的最终裁决；中途无部分校验承诺
- `unsupported_combination`（stream × response_format）错误码退场
- 网关新增一个必须与 JSON 语法保持同步的轻量监控器（实现面小，但属自有维护代码）

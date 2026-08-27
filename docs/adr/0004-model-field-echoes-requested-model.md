# 兼容响应的 model 字段返回 requested_model（偏离 OpenAI 语义）

---
status: accepted（actual_model 经扩展字段透出的部分被 ADR-0015 取代）
---

OpenAI 官方语义中，响应 `model` 字段表示实际生成内容的模型（fallback 后会变化）。本网关刻意相反：`model` 恒等于调用方传入的 requested_model，实际路由结果通过扩展字段 `actual_model` 返回。理由：调用方以请求参数做幂等键与配置比对，恒定回显更可预期；fallback 事实不隐藏，只是换了字段位置。这是明确知情的选择，不是疏忽——不要"修复"成 OpenAI 语义，除非调用方生态改变（见 CONTEXT.md 词条 requested_model / actual_model）。

## Consequences

- 与 OpenAI 官方行为不一致，严格校验 model 回显实际模型的客户端会观察到差异
- 术语全库统一为 requested_model / actual_model（不使用 resolved_model），trace 字段不变

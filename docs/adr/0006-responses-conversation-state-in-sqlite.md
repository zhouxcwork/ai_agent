# Responses 会话续接落 SQLite（网关有状态化）

---
status: accepted
---

无状态网关是更简单的推荐项，被否：作业目标包含 Responses API 的多轮对话体验（previous_response_id / store），选择把服务端会话做进网关。设计取舍：`stored_responses` 表存**展开后的完整输入 messages** 而非链式指针——续接时取父记录输入 + 其输出 + 本次新输入，O(1) 重建上下文、无递归遍历，用存储冗余换实现简单；store=false 的响应不落库（引用报 404），仅成功响应可续接；不做清理策略（开发阶段 YAGNI）。

## Consequences

- 网关从纯代理变为持有用户对话数据，隐私与保留策略成为真实议题（清理/TTL 是已知的后续项）
- 上下文长度随轮次线性膨胀存储（每条 response 存全量输入），长对话下存储成本为 O(轮数 × 平均上下文)
- SQLite 单文件适合单实例；多实例部署时会话不共享，届时需迁移存储（见 ADR-0001 的迁移提示）

# Trace 可观测字段扩展：TTFT、用量细分、缓存计价与关联 ID

---
status: accepted
---

按"一次模型调用至少记录什么"的九维度清单扩展 trace 字段（request_id 语义即 call_id，维持不变，不改名）：

- **run_id**：调用方业务链路标识，经 `X-Run-Id` 请求头透传，仅透录不解释（step 级是调用方编排概念，网关看不到边界，不记）
- **cached_tokens / reasoning_tokens**：provider 从上游 usage 的 details 防御性提取（缺失记 0）；cached 已含在 input_tokens 内，reasoning 已含在 output_tokens 内
- **ttft_ms**：**首 Token 延迟以第一个有业务意义的 delta 为准**（service 屄首个 content.delta），连接建立与上游流建立事件不计入；非流式为空；generation = latency - ttft 可推导不入库
- **finish_reason**：stop / content_filter / length 等上游结束原因（此前只透传给调用方，未入库）
- **upstream_request_id**：供应商侧请求 ID（非流式随返回携带，流式作为首个信号），排障定位上游日志的唯一钥匙；失败拿不到时为空

## 明确不做的

- **queue 延迟**：ADR 0011 快速拒绝、不排队，该维度恒为 0，不记
- **候选链与跳过原因入库**：`route_decision` 结构化日志已带 request_id 关联，trace 只记 attempts 计数，不重复存储
- **Prompt hash / Schema 版本**：(name, version) 不可变（ADR 0002）已是内容定位，hash 冗余；response_schema 是调用方内嵌资产，版本化是调用方治理职责
- **在线 Metrics 端点**：单实例内存指标价值有限；分工为 Logs（结构化 `llm_call_trace`/`route_decision` 日志）+ Trace（SQLite 表）+ 离线聚合（evals/routing_report.py）

## Considered Options

- 成本计价：cached 命中按输入全价（会系统性高估）vs 价格表加可选 `cached` 键按折扣精确计（选定，缺省键退化为输入全价，向后兼容）
- usage 细分塞进现有 Usage 模型（选定）vs 新建计价专用对象（单一消费方，过度设计）

## Consequences

- 旧库自动迁移：initialize 时按 PRAGMA table_info 缺列 ALTER 补齐，无需手工重建
- Provider 协议变更：complete 返回三元组 (content, usage, upstream_id)，stream 新增 UpstreamID 信号——所有适配方（含测试替身）需同步
- cached > input 的异常上游返回按 input 截断计价（防御）

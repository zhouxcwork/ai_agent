# 多级资源边界：租户令牌桶与并发槽，全部快速拒绝

---
status: accepted
---

> 修订（2026-08-26）：第三级并发粒度从**供应商**调整为**路由目标（供应商/模型）**，对齐上游按模型的并发配额，同供应商的不同模型互不挤占；错误码 `resource.provider_busy` 相应改为 `resource.target_busy`，配置段 `limits.providers` 改为 `limits.targets`（键 = `供应商/模型`）。

资源边界分三级，全部**快速拒绝**（429），网关侧不做排队等待：

1. **租户 QPS**：每租户令牌桶（匀速补充，容量 = 突发额度 burst），每次请求扣 1 token，桶空即拒绝 `resource.tenant_rate_limited`
2. **租户并发**：每租户在途请求计数槽，超上限拒绝 `resource.tenant_busy`；在 FastAPI yield 依赖中 acquire/release，流式请求的槽位覆盖响应全程
3. **路由目标并发**：每路由目标（`供应商/模型`）在途计数槽，在候选尝试处占用；某目标占满则跳过它尝试下一候选，全部候选因此被跳过时拒绝 `resource.target_busy`

租户身份来自调用方认证（ADR 0010 的 auth 段）：config.yaml 维护 `auth.api_keys` 白名单（apikey → tenantId），Bearer 头校验；白名单为空 = 不启用认证，租户记为 anonymous 走默认配额。

## Considered Options

- 快速拒绝（选定）：实现小、语义清楚，退避责任在调用方（SDK 普遍自带 429 退避重试）
- 排队等待队列（拒绝）：引入公平性、队列上限与取消传播的复杂度，MVP 不值
- 请求级截止时间 deadline：跨候选共享总预算，本轮**不做**（未提出需求）；`timeout_seconds` 仍是单次上游调用超时，fallback 会成倍放大总时长，后续有需求再评估
- 仅全局并发：粒度不够，单租户可挤占全部配额

## Consequences

- 令牌桶与并发槽为进程内存态：单实例部署语义正确，多实例需外置共享存储（当前 MVP 单实例）
- config.yaml 明文维护 apikey→tenantId 白名单：课程项目可接受，生产需换外部鉴权系统（接入点即该映射来源）
- 未配置 limits 段时走默认配额（qps 10 / burst 20 / 并发 5），未配置的供应商不限制
- 时钟可注入（测试确定性）；asyncio 单线程模型下桶与槽无需加锁

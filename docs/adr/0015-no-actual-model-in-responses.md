# 响应体不透出 actual_model，路由事实只记 Trace

---
status: accepted
---

ADR-0004 / ADR-0007 曾约定把实际路由目标以扩展字段 `actual_model` 随响应透出，理由是"调用方可观测性不损失"。实际使用中发现这是过度暴露：`供应商/模型` 组合泄露了后端供应商结构与协议选型，调用方一旦依赖它做比对，供应商切换、价格优化、故障切换都会变成对外的破坏性变更——这正是档位路由要隔离掉的耦合。本决策将其收回：**所有对外响应体（chat 非流式/流式 chunk、responses 非流式/流式 response 对象）一律不含 actual_model；`model` 字段仍恒回显 requested_model（ADR-0004 不变），路由事实仅记入 Trace**，经 `/v1/traces` 审计端点可查。故障排障走 Trace，不靠响应体字段。

## Considered Options

- 响应体透出 actual_model（原 ADR-0004/0007 约定）：调用方零成本观测路由，但泄露供应商架构且形成隐性依赖
- 响应体不透出，仅记 Trace（选定）：对外契约最小化，可观测性由审计端点承担
- 回退 OpenAI 原生语义（model 字段填实际模型）：与 ADR-0004 冲突，破坏回显幂等性

## Consequences

- 调用方无法从响应判断本次命中的供应商/模型；排障依赖 trace 的 `requested_model→actual_model`
- playground 测试台同步移除 actual 显示（Trace 面板保留，它读的就是审计端点）
- `/v1/models` 的 `gateway_routes[].target` 仍对外可见（管理诊断用途）；若需彻底隐藏属后续决策
- 更新 CONTEXT.md 的 actual_model 词条：从"以扩展字段返回并记入 Trace"改为"仅记入 Trace"

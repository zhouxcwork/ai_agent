# 作业二交付：为治理框架增加「转账」工具

在第二章治理框架 `tool_governance_demo.py` 基础上新增 `transfer`（转账）工具，完整跑通治理链路：
**参数校验 → 业务预检 → 权限判断 → 人工审批 → 超时处理 → 结果脱敏 → 审计追踪**。

所有新增代码均以 `# [作业二·任务 N]` 注释标注，可直接在代码内检索定位。

## 一、快速验收

环境要求：Python ≥ 3.11（用到 `asyncio.timeout`），依赖见 `requirements.txt`（`pydantic`、`pytest`）。

```bash
# 1. 运行 5 个官方验收测试（作业文档指定的验收命令）
python -m pytest tests/test_tool_governance.py -v -k "transfer"

# 2. 运行全量测试（官方 5 个 + 加固 7 个，见第四节）
python -m pytest tests/ -v

# 3. 运行离线演示，观察审批前 CONFIRM、审批后成功、账号脱敏与审计日志
python tool_governance_demo.py
```

预期结果：

- 官方验收测试：**5 passed**（含一个真实触发 2 秒策略超时的用例）。
- 全量测试：**12 passed**（总耗时约 4 秒，加固用例中另有一个真实超时场景）。
- 演示输出末尾两条转账结果与审计日志：

```json
{"tool_call_id": "call_tr_000001", "tool_name": "transfer", "ok": false, "action": "confirm", "code": "APPROVAL_REQUIRED", "content": {"message": "请人工确认本次操作的目标、金额与参数"}}
{"tool_call_id": "call_tr_000002", "tool_name": "transfer", "ok": true, "action": "allow", "code": "OK", "content": {"txn_id": "000002", "from": "ACC-A-****3456", "to": "ACC-A-****4321", "amount": 1500.0, "status": "accepted"}}
```

审计日志（`main()` 末尾打印）按 `decision`（门禁结论）/ `execution`（执行结果）两阶段记录，`execution` 记录的 `content` 已脱敏，转账两条记录为：
`decision/APPROVAL_REQUIRED`（审批前拦截）→ `decision/APPROVED`（审批核销）→ `execution/OK`（脱敏后的账务结果）。

## 二、六个任务落点

均在 `tool_governance_demo.py`（唯一改动的源码文件）：

| 任务 | 位置 | 实现要点 |
| --- | --- | --- |
| 1. 模拟账户数据 | `ACCOUNTS`（629 行） | Key 为 `(tenant_id, account_id)`，含 tenant_a 三账户（100000 / 5000 / 20000）与 tenant_b 一账户（50000）；`_INITIAL_ACCOUNTS` 初始快照供测试还原 |
| 2. 参数模型 | `TransferArgs`（46 行） | 继承 `StrictArgs`（保留 `extra="forbid"`）；账号正则 `^ACC-[A-Z]-[0-9]{6}$`，金额 `gt=0, le=100000` |
| 3. 业务预检 | `transfer_precheck`（685 行） | 只判断不改余额：`50000 < amount <= 80000` → `EXCEED_LIMIT`（教学专用区间）；余额不足 → `INSUFFICIENT_BALANCE`；超过 80000 放行以演示超时 |
| 4. 转账处理器 | `transfer_handler`（697 行） | `amount > 80000` 先 `sleep(3.0)`（超时点早于扣款，超时被取消时余额不动）；转入账户不存在 → `ACCOUNT_NOT_FOUND`；扣款段保持原子（并发不变量，见加固 1）；返回 `txn_id`（tool_call_id 后 6 位）、`from`、`to`、`amount`、`status` |
| 5. 注册工具 | `build_governance()` 工具列表末尾（803–818 行） | `WRITE` + `HIGH` + `transfer:execute` + 强制审批；`timeout_seconds=2.0`（小于演示用的 3 秒慢调用）；非幂等写超时报 `TIMEOUT_UNKNOWN` 不重试；写锁按转出账户串行化 |
| 6. 结果脱敏 | `redact()` 内账号规则（353 行） | `ACC-A-123456` → `ACC-A-****3456`（保留机构段与末 4 位），审计与返回模型的结果内容共用该函数 |

## 三、五个测试与治理链路的对应关系

| 测试（`tests/test_tool_governance.py`） | 验证的链路环节 |
| --- | --- |
| `test_transfer_rejects_injected_arguments_and_invalid_amount` | 参数校验：`extra="forbid"` 拦截注入参数（`approved`/`user_id`），Field 约束拦截非法账号、非正金额、超上限金额；余额无变化 |
| `test_transfer_precheck_blocks_over_limit_and_insufficient_balance` | 业务预检：教学区间 `EXCEED_LIMIT` 与余额不足 `INSUFFICIENT_BALANCE` 均在 handler 之前拒绝（带审批也拦不住） |
| `test_transfer_is_denied_without_permission_or_whitelist_and_in_plan_mode` | 权限判断：缺 RBAC 权限 `PERMISSION_DENIED`、不在执行白名单 `TOOL_NOT_ALLOWED`、plan 模式 `PLAN_MODE_DENIED` |
| `test_transfer_requires_approval_bound_to_arguments_and_masks_accounts` | 人工审批 + 结果脱敏 + 审计追踪：无审批 → `CONFIRM`；审批与参数摘要绑定（篡改金额被拒）；成功后账号脱敏、余额正确划转；审计两阶段序列断言；一次性审批重放被拒 |
| `test_transfer_timeout_is_reported_as_unknown_and_leaves_balances_untouched` | 超时处理：非幂等写超时报 `TIMEOUT_UNKNOWN`，实际耗时 < 3 秒，两个账户余额原封不动 |

## 三·五、加固改进（作业要求之外的补强）

官方 5 个用例之外的改进与配套回归测试（`tests/test_transfer_hardening.py`，7 个用例，与官方测试同样全部经 `runtime.invoke()`）：

| 加固项 | 位置 | 说明 |
| --- | --- | --- |
| 1. 并发不变量声明 | `transfer_handler` 扣款段注释（710 行） | 预检在写锁之外，扣款段必须保持无 `await`（原子）；写锁串行化下由预检按最新余额拦截。该约束以注释声明而非防御代码实现——当前参数空间内二次校验分支不可达亦不可测，防御代码会成为无覆盖的死分支；未来若在扣款段插入 `await`（如记流水），需改为锁内二次校验余额。并发回归测试（下述第一条）是对账失衡的报警器 |
| 2. 幂等键去重 | `TRANSFERS` 表（639 行）+ handler 幂等命中分支 | 同一 `(tenant_id, idempotency_key)` 的重复调用返回首笔回执、不重复扣款，对应"调用方超时重发"场景（审批按参数摘要绑定，重发方可再取得新凭证，幂等键是防重复扣款的唯一防线）。注意：仅做幂等返回层，未把 `policy.idempotent` 打开——框架重试只捕获 `TransientToolError`，且超时演示用例刻意要求 `TIMEOUT_UNKNOWN` 语义 |
| 3. 拆分 `TOOL_NOT_FOUND` | `before_tool_call`（430 行） | 注册表不存在的能力报 `TOOL_NOT_FOUND`，与白名单拒绝 `TOOL_NOT_ALLOWED` 区分（前者是能力未注册，后者是能力存在但本轮不给） |

对应回归测试：并发不超扣（`test_concurrent_transfers_never_overdraw`，验证写锁串行 + 一成功一拒绝 + 三账户总量守恒）、超时后账户与写锁可继续使用、同租户不存在账号与他租户账号均 `ACCOUNT_NOT_FOUND`（租户隔离）、教学区间边界（50000 放行 / 80000 右闭拦截）、`DONT_ASK` 模式拒绝强制审批、幂等键只扣一次、未注册工具 `TOOL_NOT_FOUND`。

## 四、框架适配改动说明

验收测试（`tests/test_tool_governance.py`，作业附录二）使用的框架 API 高于附录一基准代码，因此除 6 个任务外，还需将框架对齐到测试要求的形态。这部分改动均已通过全部测试验证，且不触碰治理语义本身：

| 改动 | 位置 | 原因 |
| --- | --- | --- |
| `DecisionAction = PermissionDecision` 别名 | 82 行 | 测试以 `demo.DecisionAction` 引用决策枚举；仅加别名，`PermissionEngine.decide` 未动一行 |
| `ToolResult` 增加 `code`（成功为 `OK`）与 `action`（ALLOW/CONFIRM/DENY）属性 | 171、175 行 | 原推导逻辑在 `main()` 的展示函数里，测试要求作为结果属性 |
| 审计记录对象化为 `AuditRecord`（`phase`/`code`） | 295 行 | 测试断言 `record.phase`/`record.code`；同时让 `invoke()` 统一为「每调用一条 decision + 一条 execution（成功路径）」的记录模型 |
| Handler 签名升级为 `(tool_call_id, args, ctx)` | 122 行 | 作业任务 4 自身声明的签名（`txn_id` 需由调用标识派生）；现有 3 个 handler 同步加下划线首参，`Precheck` 保持两参（任务 3 签名即两参） |
| `ApprovalStore.approve` 改位置参数 `(approval_id, ctx, tool_name, args)` | 248 行 | 测试按位置参数调用；同时兼容传入 dict 或 Pydantic 模型（摘要计算两种都支持） |
| 新增 `base_context()` / `reset_side_effects()` / `build_runtime()` | 728、740、748 行 | 测试的上下文工厂、副作用隔离与组合根入口 |
| `main()` 复用 `base_context()` 并追加转账演示与审计打印 | 827 行起 | 满足验收第二条「打印审计日志应看到账号已脱敏、审批前返回 CONFIRM」 |

另有一处文案修正：`ApprovalStore.consume` 的报错文案由写死的「请确认本次退款…」改为通用的「请人工确认本次操作的目标、金额与参数」（转账场景下原文案误导；校验逻辑与错误码未变）。

## 五、红线约束遵守情况

对照作业「五、不能改动的地方」：

1. `PermissionEngine.decide` 未改动任何一行（其优先级顺序仍是白名单 → RBAC → ALLOW 的固定框架）。
2. 所有测试调用均经 `ToolRuntime.invoke()` 完整门禁链路，无任何直调 `transfer_handler` 的路径。
3. `TransferArgs` 继承 `StrictArgs`，`extra="forbid"` 完整保留（对应测试第一个断言组）。

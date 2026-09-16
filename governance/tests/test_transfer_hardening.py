"""作业二加固测试：官方 5 个验收用例之外的正确性与边界回归。

与 tests/test_tool_governance.py（官方验收基准，保持原样不动）互补，覆盖：
- 并发一致性：同转出账户并发转账不超扣（写锁串行 + handler 锁内二次校验兜底）
- 超时恢复：TIMEOUT_UNKNOWN 之后账户与写锁可继续正常使用
- 租户隔离与账号存在性、教学区间边界、DONT_ASK 模式
- 幂等键去重：同一笔业务重复调用只扣一次款
- 错误码精确性：未注册工具报 TOOL_NOT_FOUND（区别于白名单拒绝 TOOL_NOT_ALLOWED）

约法三章与官方测试一致：所有调用都经过 `ToolRuntime.invoke`，不直接调 handler。
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Iterator, Mapping
from typing import Any

import pytest

import tool_governance_demo as demo

TRANSFER = "transfer"
FROM_ACCOUNT = "ACC-A-123456"  # tenant_a，余额 100000.0
MID_ACCOUNT = "ACC-A-654321"  # tenant_a，余额 5000.0
SMALL_ACCOUNT = "ACC-A-888888"  # tenant_a，余额 20000.0
CROSS_TENANT_ACCOUNT = "ACC-B-111111"  # tenant_b，余额 50000.0


def async_test(test: Callable[..., Awaitable[None]]) -> Callable[..., None]:
    """不依赖 pytest-asyncio，让 async 测试在任何 pytest 环境下都能跑。"""

    @functools.wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return run


def transfer_context(**overrides: Any) -> demo.ExecutionContext:
    """带 transfer:execute 权限和 transfer 执行白名单的上下文。"""

    defaults: dict[str, Any] = {
        "permissions": frozenset({"order:read", "refund:create", "shell:run", "transfer:execute"}),
        "allowed_tools": frozenset({"get_order", "create_refund", "run_shell", "transfer"}),
    }
    return demo.base_context(**{**defaults, **overrides})


def balance(account: str, tenant_id: str = "tenant_a") -> float:
    return demo.ACCOUNTS[(tenant_id, account)]


def approve(approvals: demo.ApprovalStore, arguments: Mapping[str, Any], approval_id: str) -> None:
    """对同一份参数签发一次性审批凭证（每个凭证只能核销一次）。"""

    approvals.approve(approval_id, transfer_context(), TRANSFER, dict(arguments))


async def transfer(
    runtime: demo.ToolRuntime,
    tool_call_id: str,
    arguments: Mapping[str, Any],
    **context_overrides: Any,
) -> demo.ToolResult:
    return await runtime.invoke(
        demo.ToolCall(tool_call_id, TRANSFER, arguments),
        transfer_context(**context_overrides),
    )


@pytest.fixture(autouse=True)
def isolated_side_effects() -> Iterator[None]:
    """ACCOUNTS/TRANSFERS 是模块级可变状态，逐个用例还原，保证隔离且可重复运行。"""

    accounts_snapshot = dict(demo.ACCOUNTS)
    transfers_snapshot = dict(demo.TRANSFERS)
    demo.reset_side_effects()
    yield
    demo.ACCOUNTS.clear()
    demo.ACCOUNTS.update(accounts_snapshot)
    demo.TRANSFERS.clear()
    demo.TRANSFERS.update(transfers_snapshot)


@async_test
async def test_concurrent_transfers_never_overdraw() -> None:
    """并发一致性：同一转出账户并发两笔，恰好一笔成功，余额不为负、总量守恒。

    写锁（canonical_target 按转出账户）保证执行串行，后到的一笔由预检按
    最新余额拒绝；若未来扣款段引入 await 破坏原子性，本用例是对账失衡的报警器。
    """

    runtime, approvals, _audit = demo.build_runtime()
    first = {"from_account": MID_ACCOUNT, "to_account": FROM_ACCOUNT, "amount": 3_000.0}
    second = {"from_account": MID_ACCOUNT, "to_account": SMALL_ACCOUNT, "amount": 3_000.0}
    approve(approvals, first, "approval_concurrent_1")
    approve(approvals, second, "approval_concurrent_2")

    r1, r2 = await asyncio.gather(
        transfer(runtime, "call_tr_con1", first, approval_id="approval_concurrent_1"),
        transfer(runtime, "call_tr_con2", second, approval_id="approval_concurrent_2"),
    )

    assert sorted(result.code for result in (r1, r2)) == ["INSUFFICIENT_BALANCE", "OK"]
    assert balance(MID_ACCOUNT) == 2_000.0  # 只被扣一次，不为负
    # 转账是租户内零和移动：三个账户余额总和保持 125_000 不变。
    assert balance(FROM_ACCOUNT) + balance(MID_ACCOUNT) + balance(SMALL_ACCOUNT) == 125_000.0


@async_test
async def test_transfer_still_works_after_timeout() -> None:
    """超时恢复：TIMEOUT_UNKNOWN 不占用余额也不毒化写锁，后续转账照常执行。"""

    runtime, approvals, _audit = demo.build_runtime()
    slow = {"from_account": FROM_ACCOUNT, "to_account": MID_ACCOUNT, "amount": 90_000.0}
    approve(approvals, slow, "approval_slow")
    timed_out = await transfer(runtime, "call_tr_slow", slow, approval_id="approval_slow")
    assert timed_out.code == "TIMEOUT_UNKNOWN"
    assert balance(FROM_ACCOUNT) == 100_000.0

    normal = {"from_account": FROM_ACCOUNT, "to_account": MID_ACCOUNT, "amount": 1_200.0}
    approve(approvals, normal, "approval_normal")
    ok = await transfer(runtime, "call_tr_after", normal, approval_id="approval_normal")
    assert ok.code == "OK"
    assert balance(FROM_ACCOUNT) == 98_800.0
    assert balance(MID_ACCOUNT) == 6_200.0


@async_test
async def test_transfer_to_unknown_or_cross_tenant_account_fails() -> None:
    """账号存在性 + 租户隔离：不存在的账号与他租户账号都不可作为转入方。

    ACCOUNTS 以 (tenant_id, account_id) 隔离：tenant_b 的 ACC-B-111111 对
    tenant_a 上下文不可见，与同租户不存在的账号同样报 ACCOUNT_NOT_FOUND。
    """

    runtime, approvals, _audit = demo.build_runtime()
    arguments = {"from_account": FROM_ACCOUNT, "to_account": MID_ACCOUNT, "amount": 100.0}

    # ACCOUNT_NOT_FOUND 是 handler 内的检查（作业任务 4 的位置要求），
    # 审批核销在 handler 之前，因此两个场景都要带有效审批才能走到该分支。
    unknown_args = {**arguments, "to_account": "ACC-A-999999"}
    approve(approvals, unknown_args, "approval_unknown")
    unknown = await transfer(runtime, "call_tr_unk", unknown_args, approval_id="approval_unknown")
    assert unknown.ok is False
    assert unknown.code == "ACCOUNT_NOT_FOUND"

    approve(approvals, {**arguments, "to_account": CROSS_TENANT_ACCOUNT}, "approval_cross")
    cross = await transfer(
        runtime,
        "call_tr_cross",
        {**arguments, "to_account": CROSS_TENANT_ACCOUNT},
        approval_id="approval_cross",
    )
    assert cross.ok is False
    assert cross.code == "ACCOUNT_NOT_FOUND"

    assert balance(FROM_ACCOUNT) == 100_000.0
    assert balance(CROSS_TENANT_ACCOUNT, tenant_id="tenant_b") == 50_000.0


@async_test
async def test_amount_boundaries() -> None:
    """教学区间边界：50000 不拦（严格大于才拦）、80000 拦（区间右闭）。"""

    runtime, approvals, _audit = demo.build_runtime()

    at_lower = {"from_account": FROM_ACCOUNT, "to_account": MID_ACCOUNT, "amount": 50_000.0}
    approve(approvals, at_lower, "approval_lower")
    lower = await transfer(runtime, "call_tr_50k", at_lower, approval_id="approval_lower")
    assert lower.code == "OK"
    assert balance(FROM_ACCOUNT) == 50_000.0

    at_upper = {"from_account": FROM_ACCOUNT, "to_account": MID_ACCOUNT, "amount": 80_000.0}
    upper = await transfer(runtime, "call_tr_80k", at_upper)  # 预检先于审批，无需凭证
    assert upper.ok is False
    assert upper.code == "EXCEED_LIMIT"
    assert balance(FROM_ACCOUNT) == 50_000.0


@async_test
async def test_dont_ask_mode_cannot_request_approval() -> None:
    """DONT_ASK 模式：无法请求强制审批，高风险写操作被拒绝且不产生副作用。"""

    runtime, _approvals, _audit = demo.build_runtime()
    arguments = {"from_account": FROM_ACCOUNT, "to_account": MID_ACCOUNT, "amount": 100.0}

    result = await transfer(runtime, "call_tr_dontask", arguments, mode=demo.PermissionMode.DONT_ASK)

    assert result.ok is False
    assert result.code == "APPROVAL_REQUIRED"
    assert result.action is demo.DecisionAction.CONFIRM
    assert balance(FROM_ACCOUNT) == 100_000.0


@async_test
async def test_idempotent_transfer_charges_once() -> None:
    """幂等键去重：同一笔业务（同 idempotency_key）重复调用返回首笔回执、只扣一次款。"""

    runtime, approvals, _audit = demo.build_runtime()
    arguments = {"from_account": FROM_ACCOUNT, "to_account": MID_ACCOUNT, "amount": 1_200.0}
    approve(approvals, arguments, "approval_idem_1")
    approve(approvals, arguments, "approval_idem_2")

    first = await transfer(
        runtime, "call_tr_idem01", arguments, approval_id="approval_idem_1", idempotency_key="idem-1"
    )
    second = await transfer(
        runtime, "call_tr_idem02", arguments, approval_id="approval_idem_2", idempotency_key="idem-1"
    )

    assert first.code == "OK"
    assert second.code == "OK"
    assert second.content["txn_id"] == first.content["txn_id"]  # 回执指向首次执行
    assert balance(FROM_ACCOUNT) == 98_800.0  # 只扣一次
    assert balance(MID_ACCOUNT) == 6_200.0


@async_test
async def test_unregistered_tool_reports_not_found() -> None:
    """错误码精确性：未注册的能力是 TOOL_NOT_FOUND，区别于白名单拒绝 TOOL_NOT_ALLOWED。"""

    runtime, _approvals, _audit = demo.build_runtime()
    result = await runtime.invoke(
        demo.ToolCall("call_tr_ghost", "no_such_tool", {}), transfer_context()
    )

    assert result.ok is False
    assert result.code == "TOOL_NOT_FOUND"

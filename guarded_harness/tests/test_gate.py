"""G1-G4 门禁测试：G1/G2 纯 gate 逻辑；G3/G4 走真实受控工具（工具层拦截）。"""

from conftest import call_id

from guarded_harness.approval import ApprovalError
from guarded_harness.gate import Gate, GateRejection, RepeatGuard
from guarded_harness.tools import ControlledWriteFile
from guarded_harness.types import ConsumedCall
from guarded_harness.types import RunState

SCHEMAS = {
    "read_file": {"path": str},
    "write_file": {"path": str, "content": str},
    "run_test": {"scope": str},
}


def dispatch(gate: Gate, calls: list[tuple[str, dict]], handler_log: list) -> list[GateRejection | None]:
    """模拟工具层分发：gate 拒绝则 handler 不执行（handler 调用计数的意义所在）。"""
    results = []
    for tool_name, args in calls:
        rejection = gate.check(tool_name, args)
        if rejection is None:
            handler_log.append((tool_name, args))
        results.append(rejection)
    return results


class TestG1ParamSchema:
    def test_missing_field_rejected_with_field_name(self):
        gate = Gate(SCHEMAS)
        rejection = gate.check("write_file", {"path": "a.txt"})  # 缺 content
        assert rejection is not None
        assert rejection.rule == "gate.param_schema"
        assert "content" in rejection.message

    def test_wrong_type_rejected(self):
        gate = Gate(SCHEMAS)
        rejection = gate.check("read_file", {"path": 123})
        assert rejection is not None
        assert rejection.rule == "gate.param_schema"
        assert "path" in rejection.message and "str" in rejection.message

    def test_bool_not_accepted_as_declared_type(self):
        gate = Gate(SCHEMAS)
        assert gate.check("read_file", {"path": True}) is not None

    def test_unknown_tool_rejected(self):
        gate = Gate(SCHEMAS)
        rejection = gate.check("rm_rf", {"path": "/"})
        assert rejection.rule == "gate.unknown_tool"

    def test_g1_handler_never_called_on_rejection(self):
        gate = Gate(SCHEMAS)
        handler_log: list = []
        bad_calls = [
            ("write_file", {"path": "a.txt"}),  # 缺字段
            ("read_file", {"path": 42}),  # 类型错
            ("nope", {}),  # 未知工具
        ]
        results = dispatch(gate, bad_calls, handler_log)
        assert all(r is not None for r in results)
        assert handler_log == []  # G1：handler 调用次数为 0

    def test_valid_call_passes(self):
        gate = Gate(SCHEMAS)
        assert gate.check("read_file", {"path": "src/a.py"}) is None


class TestG2Repeat:
    def test_third_identical_call_intercepted_handler_runs_twice(self):
        gate = Gate(SCHEMAS)
        handler_log: list = []
        calls = [("read_file", {"path": "x.py"})] * 3
        results = dispatch(gate, calls, handler_log)
        assert results[0] is None and results[1] is None
        assert results[2] is not None and results[2].rule == "gate.repeat"
        assert len(handler_log) == 2  # G2：handler 只执行两次

    def test_fourth_identical_still_rejected(self):
        gate = Gate(SCHEMAS)
        results = [gate.check("read_file", {"path": "x.py"}) for _ in range(4)]
        assert results[2] is not None and results[3] is not None

    def test_different_arguments_reset_counter(self):
        gate = Gate(SCHEMAS)
        assert gate.check("read_file", {"path": "a.py"}) is None
        assert gate.check("read_file", {"path": "a.py"}) is None
        assert gate.check("read_file", {"path": "b.py"}) is None  # 换参数：放行且计数重置
        assert gate.check("read_file", {"path": "b.py"}) is None
        assert gate.check("read_file", {"path": "b.py"}) is not None  # 新指纹第 3 次

    def test_different_tool_same_args_not_repeated(self):
        gate = Gate(SCHEMAS)
        assert gate.check("read_file", {"path": "x"}) is None
        assert gate.check("write_file", {"path": "x", "content": "y"}) is None

    def test_repeat_guard_state_restorable(self):
        guard = RepeatGuard()
        for _ in range(2):
            guard.observe("read_file", {"path": "x"})
        state = guard.state()
        restored = RepeatGuard(**state)
        rejection = restored.observe("read_file", {"path": "x"})
        assert rejection is not None  # 恢复后计数不清零，第 3 次仍拦截


class TestG3WaitingApproval:
    """G3：未审批的写操作 → WAITING_APPROVAL，目标文件不变（工具层挂起语义）。"""

    def test_write_pends_and_file_unchanged(self, ctx, mini):
        tool = ControlledWriteFile(ctx)
        with call_id("call-w1"):
            observation = tool(path="src/calc.py", content="def add(a, b):\n    return a - b\n", sanitize_inputs_outputs=True)
        assert observation.startswith("WAIT_APPROVAL")
        assert "proposal_id=" in observation
        assert ctx.state == RunState.WAITING_APPROVAL
        # 目标文件保持原字节：handler（真实写入）未执行
        assert (mini.workspace / "src" / "calc.py").read_text(encoding="utf-8").startswith("def add")
        assert "return a - b" not in (mini.workspace / "src" / "calc.py").read_text(encoding="utf-8")

    def test_pending_audit_record(self, ctx):
        tool = ControlledWriteFile(ctx)
        with call_id("call-w2"):
            tool(path="src/new.py", content="print(1)\n", sanitize_inputs_outputs=True)
        rec = [r for r in ctx.audit.records_for_run(ctx.run_id) if r.rule == "approval.pending"]
        assert len(rec) == 1
        assert rec[0].decision.value == "WAIT"
        assert rec[0].actor.value == "model"
        assert rec[0].tool_call_id == "call-w2"
        assert "proposal_id=" in rec[0].detail

    def test_new_file_not_created_before_approval(self, ctx, mini):
        tool = ControlledWriteFile(ctx)
        with call_id("call-w3"):
            tool(path="src/new.py", content="print(1)\n", sanitize_inputs_outputs=True)
        assert not (mini.workspace / "src" / "new.py").exists()

    def test_invalid_write_args_never_pend(self, ctx):
        """类型错误的写参数被 gate 拒绝，不进入挂起。

        注：整字段缺失在真实 agent 路径由框架 validate_tool_arguments 先行拒绝
        （同样 handler 0 次）；工具层以 None 表达"模型给了错误类型的值"。
        """
        tool = ControlledWriteFile(ctx)
        with call_id("call-w4"):
            observation = tool(path="src/x.py", content=None, sanitize_inputs_outputs=True)
        assert observation.startswith("Rejected")
        assert "content" in observation
        assert ctx.state == RunState.RUNNING
        assert ctx.audit.records_for_run(ctx.run_id)[-1].rule == "gate.param_schema"


class TestG4DuplicateDecisions:
    """G4 前半（工具/审批层）：重复批准与重复提交同一调用均拒绝；执行侧计数断言在 M4 循环。"""

    def test_second_approval_rejected(self, ctx):
        tool = ControlledWriteFile(ctx)
        with call_id("call-w5"):
            tool(path="src/a.py", content="x=1\n", sanitize_inputs_outputs=True)
        proposal = next(iter(ctx.approvals.proposals.values()))
        ctx.approvals.decide(ctx.run_id, proposal.proposal_id, approved=True)
        try:
            ctx.approvals.decide(ctx.run_id, proposal.proposal_id, approved=True)
            raised = False
        except ApprovalError:
            raised = True
        assert raised, "重复批准必须拒绝"

    def test_decide_only_pending(self, ctx):
        tool = ControlledWriteFile(ctx)
        with call_id("call-w6"):
            tool(path="src/b.py", content="x=1\n", sanitize_inputs_outputs=True)
        proposal = next(iter(ctx.approvals.proposals.values()))
        ctx.approvals.decide(ctx.run_id, proposal.proposal_id, approved=False)
        try:
            ctx.approvals.decide(ctx.run_id, proposal.proposal_id, approved=True)
            raised = False
        except ApprovalError:
            raised = True
        assert raised, "已拒绝提案不能再批准"

    def test_consume_intent_requires_approved(self, ctx):
        tool = ControlledWriteFile(ctx)
        with call_id("call-w7"):
            tool(path="src/c.py", content="x=1\n", sanitize_inputs_outputs=True)
        proposal = next(iter(ctx.approvals.proposals.values()))
        try:
            ctx.approvals.consume_intent(ctx.run_id, proposal.proposal_id, step=1)
            raised = False
        except ApprovalError:
            raised = True
        assert raised, "PENDING 提案不能直接消费"

    def test_duplicate_call_submission_rejected_after_consume(self, ctx):
        tool = ControlledWriteFile(ctx)
        with call_id("call-w8"):
            tool(path="src/d.py", content="x=1\n", sanitize_inputs_outputs=True)
        proposal = next(iter(ctx.approvals.proposals.values()))
        ctx.approvals.decide(ctx.run_id, proposal.proposal_id, approved=True)
        ctx.approvals.consume_intent(ctx.run_id, proposal.proposal_id, step=1)
        ctx.consumed_calls.append(ConsumedCall(tool_call_id=proposal.tool_call_id, tool_name="write_file",
                                               arguments_fingerprint="fp", proposal_id=proposal.proposal_id,
                                               result_status="ok"))
        with call_id("call-w8"):
            observation = tool(path="src/d.py", content="x=1\n", sanitize_inputs_outputs=True)
        assert observation.startswith("Rejected") and "already executed" in observation

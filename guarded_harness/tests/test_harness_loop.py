"""循环层集成测试：真实 ToolCallingAgent + 脚本化假模型。

覆盖：G3/G4 执行侧、批内串行与暂停后无越界、H3 预算与 StopReason、
审批→代执行→恢复全链路、模型错误暂停恢复、取消终态、A2 审计链、跨进程恢复（C3 循环级）。
"""

import subprocess
import sys

import pytest

from conftest import ScriptedModel, tool_call_msg

from guarded_harness.approval import ApprovalError, ApprovalStore
from guarded_harness.audit import AuditLog
from guarded_harness.gate import Gate
from guarded_harness.loop import HarnessLoop, ResumeRefused, build_context, load_run
from guarded_harness.sandbox import CommandSpec, Sandbox
from guarded_harness.state import CheckpointStore
from guarded_harness.tools import TOOL_SCHEMAS, RunContext, TaskSpec
from guarded_harness.types import Decision, RunState, StopReason

FIXED_CALC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"


def make_loop(mini, run_id, model, **budget) -> tuple[HarnessLoop, RunContext]:
    run_dir = mini.root / run_id
    ctx = build_context(
        run_id=run_id,
        task=TaskSpec(mode="fix", input_text="修复 sub 使测试通过", workspace_root=mini.workspace,
                      test_commands={"default": "run_pytest"}),
        run_dir=run_dir,
        gate=Gate(TOOL_SCHEMAS),
        sandbox=Sandbox(mini.workspace, mini.commands, sensitive_paths=[".env"], protected_patterns=mini.protected),
        max_turns=budget.get("max_turns", 10),
        max_writes=budget.get("max_writes", 5),
        max_tokens=budget.get("max_tokens", 10**9),
    )
    loop = HarnessLoop(ctx, model, CheckpointStore(run_dir / "state"))
    return loop, ctx


class TestG3LoopSuspension:
    def test_write_suspends_no_further_model_request(self, mini):
        model = ScriptedModel([
            tool_call_msg(("w1", "write_file", {"path": "src/calc.py", "content": FIXED_CALC})),
            tool_call_msg(("r2", "read_file", {"path": "src/calc.py"})),  # 不应被请求
        ])
        loop, ctx = make_loop(mini, "run-g3", model)
        outcome = loop.run()
        assert outcome.state == RunState.WAITING_APPROVAL
        assert len(model.generate_calls) == 1  # 挂起后无后续模型请求
        assert (mini.workspace / "src" / "calc.py").read_text(encoding="utf-8") != FIXED_CALC  # 未写入
        cp = CheckpointStore(mini.root / "run-g3" / "state").load()
        assert len(cp.pending_approvals) == 1 and cp.status == RunState.WAITING_APPROVAL  # checkpoint 含提案

    def test_batch_after_pause_no_side_effects(self, mini):
        """批内 [read, write, read]：write 挂起后同批 read 只回观察、handler 不执行。"""
        model = ScriptedModel([
            tool_call_msg(
                ("b1", "read_file", {"path": "src/calc.py"}),
                ("b2", "write_file", {"path": "src/calc.py", "content": FIXED_CALC}),
                ("b3", "read_file", {"path": "tests/test_calc_pass.py"}),
            )
        ])
        loop, ctx = make_loop(mini, "run-g3b", model)
        outcome = loop.run()
        assert outcome.state == RunState.WAITING_APPROVAL
        assert len(model.generate_calls) == 1
        observations = [getattr(s, "observations", "") for s in loop.agent.memory.steps]
        joined = "\n".join(o for o in observations if o)
        assert "SKIPPED: run paused" in joined
        # b3 的 read handler 未执行：无 b3 对应的新 source_read 证据（b1 有一条）
        reads = [e for e in ctx.evidence if e.kind == "source_read"]
        assert len(reads) == 1

    def test_call_id_bound_to_audit(self, mini):
        model = ScriptedModel([tool_call_msg(("wid-42", "write_file", {"path": "src/x.py", "content": "1"}))])
        loop, ctx = make_loop(mini, "run-g3c", model)
        loop.run()
        pending = [r for r in ctx.audit.records_for_run(ctx.run_id) if r.rule == "approval.pending"]
        assert pending[0].tool_call_id == "wid-42"  # 调用 ID 正确关联，不凭工具名猜测


class TestG4ApproveExecuteResume:
    def test_approve_executes_once_and_completes(self, mini):
        model = ScriptedModel([
            tool_call_msg(("s1", "read_file", {"path": "src/calc.py"})),
            tool_call_msg(("s2", "write_file", {"path": "src/calc.py", "content": FIXED_CALC})),
            tool_call_msg(("s3", "run_test", {"scope": "default"})),
            tool_call_msg(("s4", "final_answer", {"answer": "fixed sub"})),
        ])
        loop, ctx = make_loop(mini, "run-fix", model)
        assert loop.run().state == RunState.WAITING_APPROVAL  # read + write 两步连驱，write 挂起
        proposal_id = next(iter(ctx.approvals.proposals))
        loop.approve(proposal_id)
        outcome = loop.resume()
        assert outcome.state == RunState.COMPLETED
        assert outcome.stop_reason == StopReason.COMPLETED
        # 写入恰一次：文件为修复后内容
        assert (mini.workspace / "src" / "calc.py").read_text(encoding="utf-8") == FIXED_CALC
        executed = [r for r in ctx.audit.records_for_run(ctx.run_id)
                    if r.rule == "write.result" and r.decision == Decision.EXECUTED]
        assert len(executed) == 1
        intents = [r for r in ctx.audit.records_for_run(ctx.run_id) if r.rule == "approval.execute_intent"]
        assert len(intents) == 1
        # 测试证据：最新一次 run_test 通过（mini 项目修复后 2 用例全过）
        test_ev = [e for e in ctx.evidence if e.kind == "test_run"]
        assert test_ev[-1].passed and test_ev[-1].exit_code == 0

    def test_repeated_approval_rejected_no_second_write(self, mini):
        model = ScriptedModel([tool_call_msg(("p1", "write_file", {"path": "src/y.py", "content": "y\n"}))])
        loop, ctx = make_loop(mini, "run-g4b", model)
        loop.run()
        proposal_id = next(iter(ctx.approvals.proposals))
        loop.approve(proposal_id)
        with pytest.raises(ApprovalError):  # 重复批准：决定已记录，直接拒绝
            loop.approve(proposal_id)
        loop.resume()
        content = (mini.workspace / "src" / "y.py").read_text(encoding="utf-8")
        # 再试一次执行路径：提案已消费，重复执行拒绝，不重复写入
        with pytest.raises(ApprovalError):
            loop.approve(proposal_id)
        assert (mini.workspace / "src" / "y.py").read_text(encoding="utf-8") == content
        executed = [r for r in ctx.audit.records_for_run(ctx.run_id) if r.rule == "write.result"]
        assert len(executed) == 1

    def test_reject_feeds_model_and_continues(self, mini):
        model = ScriptedModel([
            tool_call_msg(("j1", "write_file", {"path": "src/z.py", "content": "z\n"})),
            tool_call_msg(("j2", "read_file", {"path": "src/calc.py"})),
            tool_call_msg(("j3", "read_file", {"path": "tests/test_calc_pass.py"})),
        ])
        loop, ctx = make_loop(mini, "run-rej", model)
        loop.run()
        proposal_id = next(iter(ctx.approvals.proposals))
        loop.reject(proposal_id)
        outcome = loop.resume()
        assert "REJECTED proposal" in model.last_messages()  # 模型收到拒绝理由后继续工作
        assert ctx.budget["turns_used"] >= 3
        assert not (mini.workspace / "src" / "z.py").exists()  # 拒绝后不产生写入


class TestH3Budgets:
    def test_max_turns_stops_without_fallback_final_answer(self, mini):
        model = ScriptedModel([
            tool_call_msg((f"t{i}", "read_file", {"path": "src/calc.py"})) for i in range(10)
        ])
        loop, ctx = make_loop(mini, "run-h3a", model, max_turns=3)
        outcome = loop.run()
        assert outcome.state == RunState.PAUSED
        assert outcome.stop_reason == StopReason.MAX_TURNS
        assert len(model.generate_calls) == 3  # 预算到点：无第 4 次请求，也无无工具兜底请求
        assert all(c["with_tools"] for c in model.generate_calls)
        cp = CheckpointStore(mini.root / "run-h3a" / "state").load()
        assert cp.stop_reason == StopReason.MAX_TURNS and cp.budget["turns_used"] == 3

    def test_token_budget_exhausted(self, mini):
        model = ScriptedModel(
            [tool_call_msg((f"k{i}", "read_file", {"path": "src/calc.py"})) for i in range(5)],
            token_usage=(600, 100),
        )
        loop, ctx = make_loop(mini, "run-h3b", model, max_turns=10, max_tokens=1500)
        outcome = loop.run()
        assert outcome.stop_reason == StopReason.BUDGET_EXHAUSTED
        assert outcome.state == RunState.PAUSED
        assert len(model.generate_calls) == 3  # 700/步：1400 < 1500，第 3 次后停止
        assert ctx.budget["tokens_used"] == 2100

    def test_write_budget_exhausted_after_refusal(self, mini):
        model = ScriptedModel([
            tool_call_msg(("w1", "write_file", {"path": "src/calc.py", "content": FIXED_CALC})),
            tool_call_msg(("t1", "run_test", {"scope": "default"})),
            tool_call_msg(("w2", "write_file", {"path": "src/calc.py", "content": FIXED_CALC + "#2\n"})),
            tool_call_msg(("f1", "final_answer", {"answer": "done"})),
        ])
        loop, ctx = make_loop(mini, "run-h3c", model, max_writes=1)
        loop.run()  # step1: write 挂起
        proposal_id = next(iter(ctx.approvals.proposals))
        loop.approve(proposal_id)
        loop.resume()  # 执行写 + step2 run_test + step3 write 被拒 → BUDGET_EXHAUSTED
        cp = CheckpointStore(mini.root / "run-h3c" / "state").load()
        assert cp.stop_reason == StopReason.BUDGET_EXHAUSTED
        assert cp.status == RunState.PAUSED
        assert ctx.budget["writes_used"] == 1
        assert (mini.workspace / "src" / "calc.py").read_text(encoding="utf-8") == FIXED_CALC  # 第二次写未发生

    def test_budget_paused_run_refuses_auto_resume(self, mini):
        model = ScriptedModel([tool_call_msg((f"q{i}", "read_file", {"path": "src/calc.py"})) for i in range(9)])
        loop, ctx = make_loop(mini, "run-h3d", model, max_turns=2)
        loop.run()
        outcome = loop.resume()  # 预算尽：不自动提升、不续跑
        assert outcome.stop_reason == StopReason.MAX_TURNS
        assert len(model.generate_calls) == 2


class TestModelErrorAndCancel:
    def test_model_error_pauses_then_resumes_with_memory(self, mini):
        class FlakyModel(ScriptedModel):
            def __init__(self):
                super().__init__([
                    tool_call_msg(("m1", "read_file", {"path": "src/calc.py"})),
                    tool_call_msg(("m2", "read_file", {"path": "tests/test_calc_fail.py"})),
                ])
                self.fail_next = True

            def generate(self, messages, **kwargs):
                if self.fail_next:
                    self.fail_next = False
                    self.generate_calls.append({"messages": "FAILED", "with_tools": True})
                    raise RuntimeError("provider 503")
                return super().generate(messages, **kwargs)

        model = FlakyModel()
        loop, ctx = make_loop(mini, "run-merr", model)
        outcome = loop.run()
        assert outcome.state == RunState.PAUSED and outcome.stop_reason == StopReason.MODEL_ERROR
        used = ctx.budget["turns_used"]
        outcome = loop.resume()  # 恢复：记忆保留（模型可见先前 read 观察），预算不清零
        assert outcome.stop_reason == StopReason.MODEL_ERROR  # 第二段脚本耗尽再次暂停
        # m1、m2 两步 + 耗尽那次失败的生成请求（同样消耗预算）：used + 3
        assert ctx.budget["turns_used"] == used + 3
        assert "def add" in model.last_messages()  # 先前观察仍在上下文（记忆未被清空）

    def test_cancel_is_terminal(self, mini):
        model = ScriptedModel([tool_call_msg(("c1", "write_file", {"path": "src/calc.py", "content": "x"}))])
        loop, ctx = make_loop(mini, "run-cancel", model)
        assert loop.run().state == RunState.WAITING_APPROVAL
        outcome = loop.cancel()
        assert outcome.state == RunState.CANCELLED and outcome.stop_reason == StopReason.INTERRUPTED
        with pytest.raises(ResumeRefused):
            loop.resume()


class TestA2AuditChain:
    def test_full_run_audits_chain_across_five_stages(self, mini):
        """A2：一次完整任务能按 run_id 串出 Gate→Sandbox→State→Approval→执行结果。"""
        model = ScriptedModel([
            tool_call_msg(("a1", "read_file", {"path": "src/calc.py"})),
            tool_call_msg(("a2", "write_file", {"path": "src/calc.py", "content": FIXED_CALC})),
            tool_call_msg(("a3", "run_test", {"scope": "default"})),
            tool_call_msg(("a4", "final_answer", {"answer": "fixed"})),
        ])
        loop, ctx = make_loop(mini, "run-a2", model)
        assert loop.run().state == RunState.WAITING_APPROVAL
        proposal_id = next(iter(ctx.approvals.proposals))
        loop.approve(proposal_id)
        loop.resume()
        records = ctx.audit.records_for_run(ctx.run_id)
        by_rule = {r.rule for r in records}
        # Gate（tool.read_file ALLOW/EXECUTED）、Sandbox（读写路径校验）、Approval（pending→approved→intent）、
        # 执行结果（write.result）、State（run.stopped + checkpoint 存档落地）
        assert {"tool.read_file", "approval.pending", "approval.approved",
                "approval.execute_intent", "write.result", "tool.run_test", "run.stopped"} <= by_rule
        # 每条记录都能关联到 run 与步号
        assert all(r.run_id == ctx.run_id for r in records)
        assert any(r.step == 0 for r in records)
        # 同一调用的审批链按 tool_call_id 串起
        w_chain = [r for r in records if r.tool_call_id == "a2"]
        assert [r.rule for r in w_chain] == ["approval.pending", "approval.approved", "approval.execute_intent", "write.result"]
        assert [r.decision for r in w_chain] == [Decision.WAIT, Decision.ALLOW, Decision.INTENT, Decision.EXECUTED]


class TestCrossProcessLoopResume:
    def test_pause_in_subprocess_resume_here(self, mini):
        """C3 循环级：子进程跑到 WAITING_APPROVAL 退出 → 本进程批准并恢复至完成。"""
        run_dir = mini.root / "run-c3loop"
        run_dir.mkdir(parents=True)
        code = (
            "import json, sys\n"
            "from guarded_harness.approval import ApprovalStore\n"
            "from guarded_harness.audit import AuditLog\n"
            "from guarded_harness.gate import Gate\n"
            "from guarded_harness.loop import HarnessLoop, build_context\n"
            "from guarded_harness.sandbox import CommandSpec, Sandbox\n"
            "from guarded_harness.state import CheckpointStore\n"
            "from guarded_harness.tools import TOOL_SCHEMAS, TaskSpec\n"
            "from smolagents import ChatMessage, ChatMessageToolCall, Model, MessageRole\n"
            "from smolagents.models import ChatMessageToolCallFunction\n"
            f"from pathlib import Path\n"
            f"mini_root = Path({str(mini.root)!r})\n"
            "workspace = mini_root / 'workspace'\n"
            "class M(Model):\n"
            "    def generate(self, messages, **kw):\n"
            "        return ChatMessage(role=MessageRole.ASSISTANT, content=None, tool_calls=[ChatMessageToolCall("
            "id='cp1', function=ChatMessageToolCallFunction(name='write_file', "
            "arguments={'path': 'src/calc.py', 'content': 'def add(a, b):\\n    return a + b\\n\\n\\ndef sub(a, b):\\n    return a - b\\n'}), type='function')])\n"
            "commands = {'run_pytest': CommandSpec('run_pytest', [sys.executable, '-m', 'pytest', 'tests/', '-q', '--no-header', '-p', 'no:cacheprovider'], 60.0)}\n"
            "run_dir = mini_root / 'run-c3loop'\n"
            "ctx = build_context('run-c3loop', TaskSpec('fix', 'fix sub', workspace, test_commands={'default': 'run_pytest'}), "
            "run_dir, Gate(TOOL_SCHEMAS), Sandbox(workspace, commands, [], ['tests/test_calc_pass.py', 'tests/test_calc_fail.py', 'pyproject.toml', 'requirements.lock']), 10, 5, 10**9)\n"
            "loop = HarnessLoop(ctx, M(), CheckpointStore(run_dir / 'state'))\n"
            "outcome = loop.run()\n"
            "print('OUTCOME', outcome.state.value)\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        assert "OUTCOME WAITING_APPROVAL" in proc.stdout
        assert "return a - b" not in (mini.workspace / "src" / "calc.py").read_text(encoding="utf-8")

        # 本进程：读取存档 → 批准（写日志）→ 恢复至完成
        from guarded_harness.state import CheckpointStore as CS

        cp = CS(run_dir / "state").load()
        proposal_id = cp.pending_approvals[0].proposal_id
        audit = AuditLog(run_dir / "audit" / "journal.jsonl")
        ApprovalStore(audit, {p.proposal_id: p for p in cp.pending_approvals}).decide(
            cp.run_id, proposal_id, approved=True, step=cp.step
        )
        model2 = ScriptedModel([
            tool_call_msg(("cp2", "run_test", {"scope": "default"})),
            tool_call_msg(("cp3", "final_answer", {"answer": "fixed via resume"})),
        ])
        loop2 = load_run(
            run_dir, model2,
            gate=Gate(TOOL_SCHEMAS),
            sandbox=Sandbox(mini.workspace, mini.commands, sensitive_paths=[".env"], protected_patterns=mini.protected),
            task=TaskSpec(mode="fix", input_text="fix sub", workspace_root=mini.workspace,
                          test_commands={"default": "run_pytest"}),
        )
        outcome = loop2.resume()
        assert outcome.state == RunState.COMPLETED
        assert "return a - b" in (mini.workspace / "src" / "calc.py").read_text(encoding="utf-8")  # 写入完成
        executed = [r for r in loop2.ctx.audit.records_for_run(cp.run_id) if r.rule == "write.result"]
        assert len(executed) == 1  # 不重复写入
        assert loop2.ctx.budget["turns_used"] == 3  # 计数不清零：子进程 1 步 + 本进程 2 步


class TestRecoveryOrchestration:
    """loop 级恢复编排：C2 悬挂 INTENT 与 C4 工作区变化都走 load_run 保守停止。"""

    def _pause_with_pending_write(self, mini, model) -> HarnessLoop:
        loop, ctx = make_loop(mini, "run-rc", model)
        assert loop.run().state == RunState.WAITING_APPROVAL
        return loop

    def test_dangling_intent_resume_blocks_without_replay(self, mini):
        """C2 loop 级：INTENT 落盘后崩溃 → resume 拒绝自动重放，NEEDS_REVIEW。"""
        model = ScriptedModel([
            tool_call_msg(("d1", "write_file", {"path": "src/calc.py", "content": FIXED_CALC})),
        ])
        loop, ctx = make_loop(mini, "run-c2l", model)
        loop.run()
        proposal_id = next(iter(ctx.approvals.proposals))
        loop.approve(proposal_id)
        # 模拟崩溃窗口：直接持久化 INTENT（消费凭据）但不执行 handler、不落结果
        ctx.approvals.consume_intent(ctx.run_id, proposal_id, step=ctx.step)
        run_dir = mini.root / "run-c2l"
        model2 = ScriptedModel([tool_call_msg(("d2", "run_test", {"scope": "default"}))])
        loop2 = load_run(
            run_dir, model2, gate=Gate(TOOL_SCHEMAS),
            sandbox=Sandbox(mini.workspace, mini.commands, sensitive_paths=[".env"], protected_patterns=mini.protected),
            task=TaskSpec(mode="fix", input_text="fix", workspace_root=mini.workspace,
                          test_commands={"default": "run_pytest"}),
        )
        outcome = loop2.resume()
        assert outcome.state == RunState.PAUSED and outcome.stop_reason == StopReason.NEEDS_REVIEW
        assert len(model2.generate_calls) == 0  # 不启动模型、不重放
        assert "return a - b" not in (mini.workspace / "src" / "calc.py").read_text(encoding="utf-8")

    def test_workspace_changed_after_checkpoint_blocks(self, mini):
        """C4 loop 级：外部改文件 → load_run 即 NEEDS_REVIEW，旧证据/审批失效。"""
        model = ScriptedModel([
            tool_call_msg(("e1", "write_file", {"path": "src/calc.py", "content": FIXED_CALC})),
        ])
        loop, ctx = make_loop(mini, "run-c4l", model)
        loop.run()  # 挂起并存档
        (mini.workspace / "src" / "calc.py").write_text("tampered externally\n", encoding="utf-8")
        model2 = ScriptedModel([])
        loop2 = load_run(
            mini.root / "run-c4l", model2, gate=Gate(TOOL_SCHEMAS),
            sandbox=Sandbox(mini.workspace, mini.commands, sensitive_paths=[".env"], protected_patterns=mini.protected),
            task=TaskSpec(mode="fix", input_text="fix", workspace_root=mini.workspace,
                          test_commands={"default": "run_pytest"}),
        )
        outcome = loop2.resume()
        assert outcome.state == RunState.PAUSED and outcome.stop_reason == StopReason.NEEDS_REVIEW
        assert len(model2.generate_calls) == 0

    def test_persistence_failure_stops_progress(self, mini):
        """关键存档失败：当前进程停止推进（FAILED），无后续模型调用。"""
        model = ScriptedModel([tool_call_msg(("f1", "read_file", {"path": "src/calc.py"}))])
        loop, ctx = make_loop(mini, "run-pf", model)
        state_dir = mini.root / "run-pf" / "state"
        state_dir.rmdir()  # 用同名文件顶替目录 → save 的 mkdir 失败
        state_dir.write_text("not a dir", encoding="utf-8")
        with pytest.raises(OSError):
            loop.run()
        assert ctx.state == RunState.FAILED
        gens_before = len(model.generate_calls)
        outcome = loop.run()  # FAILED 终态：不再推进
        assert outcome.state == RunState.FAILED
        assert len(model.generate_calls) == gens_before


def test_rejected_decision_feedback_survives_reload(mini):
    from guarded_harness.loop import load_run

    model = ScriptedModel([tool_call_msg(("reject-me", "write_file", {"path": "src/calc.py", "content": "new"}))])
    loop, ctx = make_loop(mini, "reload-rejection", model)
    loop.run()
    proposal = next(iter(ctx.approvals.proposals))
    # 模拟独立 approve/reject 命令：仅决定写入日志，不更新旧 checkpoint。
    ctx.approvals.decide(ctx.run_id, proposal, approved=False)
    restored = load_run(mini.root / "reload-rejection", ScriptedModel([]),
                        gate=Gate(TOOL_SCHEMAS), sandbox=ctx.sandbox, task=ctx.task)
    assert any("REJECTED proposal" in (getattr(s, "observations", "") or "")
               for s in restored.agent.memory.steps)
    assert restored.plan == loop.plan and restored.plan


def test_budget_stop_reason_survives_reload(mini):
    from guarded_harness.loop import load_run

    model = ScriptedModel([tool_call_msg(("r", "read_file", {"path": "src/calc.py"}))])
    loop, ctx = make_loop(mini, "reload-budget", model, max_turns=1)
    assert loop.run().stop_reason == StopReason.MAX_TURNS
    next_model = ScriptedModel([])
    restored = load_run(mini.root / "reload-budget", next_model,
                        gate=Gate(TOOL_SCHEMAS), sandbox=ctx.sandbox, task=ctx.task)
    assert restored.resume().stop_reason == StopReason.MAX_TURNS
    assert not next_model.generate_calls


def test_real_process_exit_after_write_before_result(mini):
    import multiprocessing
    import os
    from guarded_harness.loop import load_run

    model = ScriptedModel([tool_call_msg(("crash-write", "write_file", {"path": "src/calc.py", "content": "# written before crash\n"}))])
    loop, ctx = make_loop(mini, "crash-window", model)
    loop.run()
    loop.approve(next(iter(ctx.approvals.proposals)))

    def crash_after_write():
        original = loop._write_file

        def write_then_exit(path, content):
            ok, _ = original(path, content)
            os._exit(137 if ok else 138)

        loop._write_file = write_then_exit
        loop.resume()

    child = multiprocessing.get_context("fork").Process(target=crash_after_write)
    child.start()
    child.join(10)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("crash probe did not exit")
    assert child.exitcode == 137
    assert (mini.workspace / "src/calc.py").read_text() == "# written before crash\n"
    next_model = ScriptedModel([])
    restored = load_run(mini.root / "crash-window", next_model,
                        gate=Gate(TOOL_SCHEMAS), sandbox=ctx.sandbox, task=ctx.task)
    assert restored.resume().stop_reason == StopReason.NEEDS_REVIEW
    assert not next_model.generate_calls
    assert (mini.workspace / "src/calc.py").read_text() == "# written before crash\n"


def test_write_does_not_follow_predictable_temporary_symlink(mini):
    outside = mini.root / "outside.txt"
    outside.write_text("keep")
    target = mini.workspace / "src" / "output.py"
    target.with_name(target.name + ".tmp").symlink_to(outside)
    assert HarnessLoop._write_file(target, "approved content")[0]
    assert outside.read_text() == "keep"
    assert target.read_text() == "approved content"


def test_audit_failure_stops_model_and_remaining_batch(mini, monkeypatch):
    import guarded_harness.audit as audit_module

    model = ScriptedModel([
        tool_call_msg(("r1", "read_file", {"path": "src/calc.py"}),
                      ("w1", "write_file", {"path": "src/calc.py", "content": "bad"})),
        tool_call_msg(("r2", "read_file", {"path": "src/calc.py"})),
    ])
    loop, ctx = make_loop(mini, "audit-failure", model)
    def fail(_):
        raise OSError("audit fsync failure")
    monkeypatch.setattr(audit_module.os, "fsync", fail)
    result = loop.run()
    assert result.state == RunState.FAILED
    assert result.stop_reason == StopReason.PERSISTENCE_FAILED
    assert len(model.generate_calls) == 1
    assert not ctx.approvals.proposals


def test_sequence_increases_across_loop_and_reload(mini):
    model = ScriptedModel([tool_call_msg(("w", "write_file", {"path": "src/calc.py", "content": "new"}))])
    loop, ctx = make_loop(mini, "seq-check", model)
    loop.run()
    first = loop.store.load().seq
    loop.run()  # still waiting; explicit save exercises next checkpoint
    loop._save(None)
    assert loop.store.load().seq > first
    restored = load_run(mini.root / "seq-check", ScriptedModel([]),
                        gate=Gate(TOOL_SCHEMAS), sandbox=ctx.sandbox, task=ctx.task)
    second = loop.store.load().seq
    restored.resume()
    assert restored.store.load().seq > second


def test_cli_decision_is_once_across_invocations(mini):
    import argparse
    import json
    from guarded_harness.__main__ import _decide, EXIT_DONE, EXIT_BAD

    model = ScriptedModel([tool_call_msg(("w", "write_file", {"path": "src/calc.py", "content": "new"}))])
    loop, ctx = make_loop(mini, "decide-once", model)
    loop.run()
    run_dir = mini.root / "decide-once"
    (run_dir / "run_config.json").write_text(json.dumps({}))
    args = argparse.Namespace(run_dir=str(run_dir), proposal_id=next(iter(ctx.approvals.proposals)), by="user")
    assert _decide(args, True) == EXIT_DONE
    assert _decide(args, True) == EXIT_BAD
    assert _decide(args, False) == EXIT_BAD


def test_reloaded_approved_write_requires_resume(mini):
    model = ScriptedModel([tool_call_msg(("w", "write_file", {"path": "src/calc.py", "content": "# approved\n"}))])
    loop, ctx = make_loop(mini, "approved-run-guard", model)
    loop.run()
    ctx.approvals.decide(ctx.run_id, next(iter(ctx.approvals.proposals)), approved=True)
    next_model = ScriptedModel([])
    restored = load_run(mini.root / "approved-run-guard", next_model,
                        gate=Gate(TOOL_SCHEMAS), sandbox=ctx.sandbox, task=ctx.task)
    before = (mini.workspace / "src/calc.py").read_text()
    assert restored.run().state == RunState.WAITING_APPROVAL
    assert not next_model.generate_calls
    assert (mini.workspace / "src/calc.py").read_text() == before


@pytest.mark.parametrize("name,args,rule", [
    ("read_file", {}, "gate.param_schema"),
    ("read_file", {"path": 123}, "gate.param_schema"),
    ("unknown_tool", {}, "gate.unknown_tool"),
])
def test_framework_rejections_have_call_audit(mini, name, args, rule):
    model = ScriptedModel([tool_call_msg(("rejected-call", name, args))])
    loop, ctx = make_loop(mini, "framework-reject", model, max_turns=1)
    loop.run()
    records = [r for r in ctx.audit.records_for_run(ctx.run_id) if r.tool_call_id == "rejected-call"]
    assert len(records) == 1
    assert records[0].rule == rule
    assert records[0].decision == Decision.REJECT
    assert not ctx.evidence and not ctx.approvals.proposals

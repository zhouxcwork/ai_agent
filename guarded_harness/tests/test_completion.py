"""完成判定测试（H1/H2 + understand 模式边界）。"""

import pytest

from conftest import ScriptedModel, tool_call_msg

from guarded_harness.completion import check_completion
from guarded_harness.gate import Gate
from guarded_harness.loop import HarnessLoop, build_context
from guarded_harness.sandbox import Sandbox
from guarded_harness.state import CheckpointStore
from guarded_harness.tools import TOOL_SCHEMAS, ControlledWriteFile, RunContext, TaskSpec
from guarded_harness.types import Evidence, RunState, StopReason

from conftest import call_id

FIXED_CALC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"


def make_loop(mini, run_id, model, *, mode="fix", **kwargs) -> tuple[HarnessLoop, RunContext]:
    run_dir = mini.root / run_id
    task = TaskSpec(
        mode=mode,
        input_text="任务",
        workspace_root=mini.workspace,
        artifact_target="artifacts/login.md" if mode == "understand" else "artifacts/result.md",
        write_allow_patterns=["artifacts/**"] if mode == "understand" else None,
        test_commands={"default": "run_pytest"} if mode == "fix" else {},
    )
    ctx = build_context(
        run_id=run_id, task=task, run_dir=run_dir, gate=Gate(TOOL_SCHEMAS),
        sandbox=Sandbox(mini.workspace, mini.commands, sensitive_paths=[".env"], protected_patterns=mini.protected),
        max_turns=kwargs.get("max_turns", 10), max_writes=kwargs.get("max_writes", 5),
        max_tokens=kwargs.get("max_tokens", 10**9),
    )
    return HarnessLoop(ctx, model, CheckpointStore(run_dir / "state")), ctx


class TestH1NoDeliverable:
    def test_unit_check_requires_evidence(self, ctx):
        with pytest.raises(ValueError, match="no test evidence"):
            check_completion(ctx)

    def test_final_answer_rejected_loop_continues(self, mini):
        """模型说完成但无交付物：final answer 被拒，不能进入完成态（循环继续）。"""
        model = ScriptedModel([
            tool_call_msg(("h1a", "final_answer", {"answer": "done, tests pass"})),  # 无证据 → 拒
            tool_call_msg(("h1b", "final_answer", {"answer": "really done"})),  # 仍无证据 → 再拒
        ])
        loop, ctx = make_loop(mini, "run-h1", model, max_turns=2)
        outcome = loop.run()
        assert outcome.state == RunState.PAUSED  # 不是 COMPLETED
        assert outcome.stop_reason == StopReason.MAX_TURNS
        assert len(model.generate_calls) == 2  # 拒绝后循环继续（第二次模型请求发生）
        errors = [s for s in loop.agent.memory.steps if getattr(s, "error", None)]
        assert errors and all("guarded_completion_check" in str(s.error) for s in errors)

    def test_failed_latest_evidence_rejects(self, ctx):
        ctx.evidence.append(Evidence(kind="test_run", scope="default", passed=False, exit_code=1,
                                      content_digest="d0"))
        with pytest.raises(ValueError, match="failed"):
            check_completion(ctx)


class TestH2StaleEvidence:
    def _passing_evidence(self, ctx):
        digest = ctx.sandbox.digest_fileset(ctx.task.evidence_patterns, ctx.task.evidence_excludes)
        ctx.evidence.append(Evidence(kind="test_run", scope="default", passed=True, exit_code=0,
                                     content_digest=digest))
        return digest

    def test_fresh_passing_evidence_accepted(self, ctx):
        self._passing_evidence(ctx)
        check_completion(ctx)  # 不抛

    def test_source_change_invalidates_evidence(self, ctx, mini):
        self._passing_evidence(ctx)
        (mini.workspace / "src" / "calc.py").write_text("def add(a, b):\n    return 42\n", encoding="utf-8")
        with pytest.raises(ValueError, match="stale"):
            check_completion(ctx)

    def test_same_content_restored_evidence_valid_again(self, ctx, mini):
        digest = self._passing_evidence(ctx)
        original = (mini.workspace / "src" / "calc.py").read_text(encoding="utf-8")
        (mini.workspace / "src" / "calc.py").write_text("changed", encoding="utf-8")
        (mini.workspace / "src" / "calc.py").write_text(original, encoding="utf-8")  # 改回同内容
        assert ctx.sandbox.digest_fileset(ctx.task.evidence_patterns, ctx.task.evidence_excludes) == digest
        check_completion(ctx)

    def test_latest_failed_beats_older_pass(self, ctx, mini):
        """同内容下先通过后失败：只认最新一次，旧通过不能用于完成。"""
        digest = self._passing_evidence(ctx)
        ctx.evidence.append(Evidence(kind="test_run", scope="default", passed=False, exit_code=1,
                                     content_digest=digest))  # 更新一次失败记录（内容未变）
        with pytest.raises(ValueError, match="failed"):
            check_completion(ctx)

    def test_write_after_pass_forces_rerun_end_to_end(self, mini):
        """测试通过后又修改源码：完成检查拒绝（stale），必须重跑当前版本。"""
        model = ScriptedModel([
            tool_call_msg(("w1", "write_file", {"path": "src/calc.py", "content": FIXED_CALC})),
            tool_call_msg(("t1", "run_test", {"scope": "default"})),  # 修复后：PASS（digest D2）
            tool_call_msg(("w2", "write_file", {"path": "src/calc.py", "content": FIXED_CALC + "# tweak\n"})),
            # w2 执行后 t1 证据过期；f1 将被拒，被迫重跑
            tool_call_msg(("f1", "final_answer", {"answer": "fixed"})),
            tool_call_msg(("t2", "run_test", {"scope": "default"})),  # 重跑当前版本：PASS
            tool_call_msg(("f2", "final_answer", {"answer": "fixed"})),
        ])
        loop, ctx = make_loop(mini, "run-h2", model, max_writes=2)
        assert loop.run().state == RunState.WAITING_APPROVAL  # w1 → 挂起
        pid1 = next(iter(ctx.approvals.proposals))
        loop.approve(pid1)
        loop.resume()  # 执行 w1 + t1(PASS) + w2 → 再次挂起
        assert ctx.state == RunState.WAITING_APPROVAL
        pid2 = next(pid for pid, p in ctx.approvals.proposals.items() if p.tool_call_id == "w2")
        loop.approve(pid2)
        outcome = loop.resume()
        assert outcome.state == RunState.COMPLETED
        # f1 被 stale 拒绝的痕迹保留在记忆错误里
        errors = [str(getattr(s, "error", "")) for s in loop.agent.memory.steps]
        assert any("stale" in e for e in errors)
        runs = [e for e in ctx.evidence if e.kind == "test_run"]
        assert len(runs) == 2 and runs[0].passed is True and runs[1].passed is True
        assert runs[0].content_digest != runs[1].content_digest  # 两次证据绑不同内容版本


class TestUnderstandMode:
    def test_artifact_missing_rejected(self, ctx):  # ctx 为 fix 模式，构造 understand 检查
        from guarded_harness.tools import TaskSpec

        ctx.task = TaskSpec(mode="understand", input_text="t", workspace_root=ctx.task.workspace_root,
                            artifact_target="artifacts/login.md", write_allow_patterns=["artifacts/**"])
        ctx.evidence.append(Evidence(kind="source_read", scope="x"))
        with pytest.raises(ValueError, match="missing or empty"):
            check_completion(ctx)

    def test_full_understand_flow(self, mini):
        model = ScriptedModel([
            tool_call_msg(("u1", "read_file", {"path": "src/calc.py"})),
            tool_call_msg(("u-wrong", "write_file", {"path": "artifacts/wrong.md", "content": "wrong"})),
            tool_call_msg(("u2", "write_file", {"path": "artifacts/login.md", "content": "# 登录逻辑说明\nsub 存在 bug。\n"})),
            tool_call_msg(("u3", "final_answer", {"answer": "文档已生成"})),
        ])
        loop, ctx = make_loop(mini, "run-und", model, mode="understand")
        assert loop.run().state == RunState.WAITING_APPROVAL
        assert len(ctx.approvals.proposals) == 1
        assert any(r.tool_call_id == "u-wrong" and r.rule == "sandbox.write_out_of_scope"
                   for _, r in ctx.audit.read_all())
        proposal_id = next(iter(ctx.approvals.proposals))
        assert not (mini.workspace / "artifacts" / "login.md").exists()
        loop.approve(proposal_id)
        outcome = loop.resume()
        assert outcome.state == RunState.COMPLETED
        artifact = mini.workspace / "artifacts" / "login.md"
        assert artifact.exists() and artifact.stat().st_size > 0
        assert not (mini.workspace / "artifacts" / "wrong.md").exists()

    def test_approved_artifact_cannot_be_redirected(self, mini):
        model = ScriptedModel([
            tool_call_msg(("u-link", "write_file", {"path": "artifacts/login.md", "content": "approved"})),
        ])
        loop, ctx = make_loop(mini, "run-und-link", model, mode="understand")
        assert loop.run().state == RunState.WAITING_APPROVAL
        loop.approve(next(iter(ctx.approvals.proposals)))
        target = mini.workspace / "artifacts" / "login.md"
        target.parent.mkdir(exist_ok=True)
        target.symlink_to("wrong.md")
        outcome = loop.resume()
        assert outcome.state == RunState.PAUSED
        assert outcome.stop_reason == StopReason.NEEDS_REVIEW
        assert not (target.parent / "wrong.md").exists()
        assert any(r.tool_call_id == "u-link" and r.rule == "approval.revalidation_failed"
                   for _, r in ctx.audit.read_all())

    def test_source_write_rejected_in_understand_mode(self, ctx):
        """代码理解：源码不可写，仅 artifacts 可经审批生成。"""
        from guarded_harness.tools import TaskSpec

        ctx.task = TaskSpec(mode="understand", input_text="t", workspace_root=ctx.task.workspace_root,
                            artifact_target="artifacts/login.md", write_allow_patterns=["artifacts/**"])
        tool = ControlledWriteFile(ctx)
        with call_id("uw1"):
            observation = tool(path="src/calc.py", content="evil\n", sanitize_inputs_outputs=True)
        assert "understand mode" in observation and observation.startswith("Rejected")
        assert "return a + b" in (ctx.task.workspace_root / "src" / "calc.py").read_text(encoding="utf-8")


def test_fix_requires_configured_tests_and_deliverable(ctx):
    ctx.task.test_commands = {}
    with pytest.raises(ValueError, match="no test commands"):
        check_completion(ctx)
    ctx.task.test_commands = {"default": "run_pytest"}
    digest = ctx.sandbox.digest_fileset(ctx.task.evidence_patterns, ctx.task.evidence_excludes)
    ctx.evidence.append(Evidence(kind="test_run", scope="default", passed=True, exit_code=0, content_digest=digest))
    (ctx.task.workspace_root / ctx.task.artifact_target).unlink()
    with pytest.raises(ValueError, match="missing or empty"):
        check_completion(ctx)


def test_config_change_invalidates_test_evidence(ctx):
    digest = ctx.sandbox.digest_fileset(ctx.task.evidence_patterns, ctx.task.evidence_excludes)
    ctx.evidence.append(Evidence(kind="test_run", scope="default", passed=True, exit_code=0, content_digest=digest))
    (ctx.task.workspace_root / "pyproject.toml").write_text("# changed config")
    with pytest.raises(ValueError, match="stale"):
        check_completion(ctx)


@pytest.mark.parametrize("limit,expected", [(150, RunState.PAUSED), (200, RunState.PAUSED), (201, RunState.COMPLETED)])
def test_final_response_token_budget_rechecked(mini, limit, expected):
    model = ScriptedModel([tool_call_msg(("f", "final_answer", {"answer": "done"}))], token_usage=(100, 100))
    loop, ctx = make_loop(mini, f"final-token-{limit}", model, max_tokens=limit)
    digest = ctx.sandbox.digest_fileset(ctx.task.evidence_patterns, ctx.task.evidence_excludes)
    ctx.evidence.append(Evidence(kind="test_run", scope="default", passed=True, exit_code=0, content_digest=digest))
    result = loop.run()
    assert result.state == expected
    assert ctx.budget["tokens_used"] == 200
    assert len(model.generate_calls) == 1
    if expected == RunState.PAUSED:
        assert result.stop_reason == StopReason.BUDGET_EXHAUSTED


def test_root_source_change_blocks_restore_and_completion(ctx):
    from guarded_harness.state import snapshot, workspace_changed

    main = ctx.task.workspace_root / "main.py"
    main.write_text("answer = 1\n")
    cp = snapshot(ctx)
    digest = ctx.sandbox.digest_fileset(ctx.task.evidence_patterns, ctx.task.evidence_excludes)
    ctx.evidence.append(Evidence(kind="test_run", scope="default", passed=True, exit_code=0, content_digest=digest))
    main.write_text("answer = 0\n")
    assert workspace_changed(cp, ctx.sandbox, ctx.task)
    with pytest.raises(ValueError, match="stale"):
        check_completion(ctx)

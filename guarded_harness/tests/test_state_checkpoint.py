"""C1-C4 检查点与恢复对齐测试（真实子进程跨进程保存/恢复）。"""

import json
import subprocess
import sys

import pytest

from conftest import TOOL_SCHEMAS, call_id

from smolagents import Timing

from guarded_harness.approval import ApprovalStore
from guarded_harness.audit import AuditLog
from guarded_harness.gate import Gate, RepeatGuard
from guarded_harness.sandbox import Sandbox
from guarded_harness.state import (
    CheckpointCorruptedError,
    CheckpointStore,
    align_recovery,
    apply_to_context,
    dump_memory,
    load_memory,
    snapshot,
    workspace_changed,
)
from guarded_harness.tools import ControlledWriteFile, RunContext, TaskSpec
from guarded_harness.types import Actor, AuditRecord, ConsumedCall, Decision, Evidence


def fresh_ctx(mini, run_id="run-c") -> RunContext:
    run_dir = mini.root / run_id
    audit = AuditLog(run_dir / "audit" / "journal.jsonl")
    return RunContext(
        run_id=run_id,
        task=TaskSpec(mode="fix", input_text="t", workspace_root=mini.workspace,
                      test_commands={"default": "run_pytest"}),
        gate=Gate(TOOL_SCHEMAS),
        sandbox=Sandbox(mini.workspace, mini.commands, sensitive_paths=[".env"], protected_patterns=mini.protected),
        audit=audit,
        approvals=ApprovalStore(audit),
        max_turns=10,
        max_writes=5,
        max_tokens=100000,
    )


def make_pending_write(ctx: RunContext, path="src/new.py", cid="call-x") -> str:
    tool = ControlledWriteFile(ctx)
    with call_id(cid):
        observation = tool(path=path, content="x=1\n", sanitize_inputs_outputs=True)
    assert observation.startswith("WAIT_APPROVAL")
    return next(iter(ctx.approvals.proposals.values())).proposal_id


class TestC1Checkpoint:
    def test_snapshot_contains_six_essentials(self, ctx, mini):
        make_pending_write(ctx)
        ctx.step = 3
        ctx.budget["turns_used"] = 2
        ctx.budget["writes_used"] = 1
        ctx.evidence.append(Evidence(kind="test_run", scope="default", passed=True))
        cp = snapshot(ctx, plan="先读后改再测", current_call_id="call-x")
        assert cp.run_id == ctx.run_id
        assert cp.step == 3
        assert cp.plan == "先读后改再测"
        assert len(cp.evidence) == 1
        assert cp.budget["turns_used"] == 2 and cp.budget["max_turns"] == 10
        assert len(cp.pending_approvals) == 1  # 待审批动作在档
        assert cp.task["mode"] == "fix"
        assert cp.workspace_digest

    def test_save_load_roundtrip_and_seq_monotonic(self, ctx, tmp_path):
        store = CheckpointStore(tmp_path / "state")
        cp = snapshot(ctx, plan="p")
        store.save(cp)
        assert cp.seq == 1
        first = store.load()
        assert first.seq == 1 and first.plan == "p"
        cp.plan = "p2"
        store.save(cp)
        assert cp.seq == 2 and store.load().plan == "p2"
        assert not (tmp_path / "state" / "checkpoint.json.tmp").exists()  # 不留半写临时文件

    def test_checksum_detects_tampering(self, ctx, tmp_path):
        store = CheckpointStore(tmp_path / "state")
        store.save(snapshot(ctx, plan="original"))
        path = tmp_path / "state" / "checkpoint.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["plan"] = "tampered"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(CheckpointCorruptedError):
            store.load()

    def test_apply_restores_runtime(self, ctx, mini):
        make_pending_write(ctx, cid="call-r")
        ctx.step = 5
        ctx.budget["writes_used"] = 2
        ctx.consumed_calls.append(
            ConsumedCall(tool_call_id="call-old", tool_name="write_file", arguments_fingerprint="fp",
                         proposal_id="p1", result_status="ok")
        )
        cp = snapshot(ctx)
        ctx2 = fresh_ctx(mini)
        apply_to_context(cp, ctx2)
        assert ctx2.step == 5
        assert ctx2.budget["writes_used"] == 2
        assert ctx2.consumed_call_ids == {"call-old"}
        assert ctx2.gate.repeat_guard.consecutive_count == 1  # write 调用观察过一次
        assert set(ctx2.approvals.proposals) == set(ctx.approvals.proposals)


class TestC2DanglingIntent:
    def test_dangling_intent_marks_unknown_result(self, ctx):
        pid = make_pending_write(ctx, cid="call-c2a")
        cp = snapshot(ctx)  # 存档点：提案已登记（cursor 覆盖 WAIT 记录）
        ctx.approvals.decide(ctx.run_id, pid, approved=True)
        ctx.approvals.consume_intent(ctx.run_id, pid, step=1)  # INTENT 已落盘
        # 崩溃：真实写入已发生但结果未持久化 → 恢复对齐必须发现
        alignment = align_recovery(cp, ctx.audit)
        assert alignment.unknown_result_calls == ["call-c2a"]
        assert "dangling" in alignment.block_reason

    def test_pending_without_intent_not_misjudged(self, ctx):
        """正常待审批（仅 WAIT，无 INTENT）恢复不误判为悬挂写入。"""
        make_pending_write(ctx, cid="call-c2b")
        cp = snapshot(ctx)
        alignment = align_recovery(cp, ctx.audit)
        assert alignment.block_reason == ""
        assert alignment.unknown_result_calls == []

    def test_result_persisted_but_checkpoint_stale_blocks(self, ctx):
        """执行结果已落盘但 checkpoint 未更新 → 保守 NEEDS_REVIEW，不自动重建。"""
        pid = make_pending_write(ctx, cid="call-c2c")
        cp = snapshot(ctx)
        ctx.approvals.decide(ctx.run_id, pid, approved=True)
        ctx.approvals.consume_intent(ctx.run_id, pid, step=1)
        ctx.record("write.result", Decision.EXECUTED, Actor.HARNESS, "call-c2c ok", tool_call_id="call-c2c")
        alignment = align_recovery(cp, ctx.audit)
        assert alignment.unknown_result_calls == []
        assert "manual review" in alignment.block_reason

    def test_corrupt_journal_tail_refuses_recovery(self, ctx, tmp_path):
        make_pending_write(ctx, cid="call-c2d")
        cp = snapshot(ctx)
        with open(ctx.audit.path, "a", encoding="utf-8") as fh:
            fh.write('{"torn')  # 半写
        alignment = align_recovery(cp, ctx.audit)
        assert "journal corrupt" in alignment.block_reason


class TestC4WorkspaceChanged:
    def test_external_change_detected(self, ctx, mini):
        cp = snapshot(ctx)
        assert workspace_changed(cp, ctx.sandbox, ctx.task) is False
        (mini.workspace / "src" / "calc.py").write_text("def add(a, b):\n    return 99\n", encoding="utf-8")
        assert workspace_changed(cp, ctx.sandbox, ctx.task) is True

    def test_state_changes_do_not_affect_digest(self, ctx):
        cp = snapshot(ctx)
        ctx.audit.append(AuditRecord(
            run_id=ctx.run_id, step=1, tool_call_id="c", rule="r", decision=Decision.CHECKPOINT,
            actor=Actor.HARNESS, timestamp=0.0))
        assert workspace_changed(cp, ctx.sandbox, ctx.task) is False


class TestC3CrossProcess:
    def test_subprocess_save_then_restore_constraints(self, mini, tmp_path):
        """真实子进程保存存档并退出；本进程恢复：计数/预算/消费不清零、不重复。"""
        work = tmp_path / "c3"
        code = (
            "import sys\n"
            "from guarded_harness.approval import ApprovalStore\n"
            "from guarded_harness.audit import AuditLog\n"
            "from guarded_harness.gate import Gate, RepeatGuard\n"
            "from guarded_harness.sandbox import CommandSpec, Sandbox\n"
            "from guarded_harness.state import CheckpointStore, snapshot\n"
            "from guarded_harness.tools import RunContext, TaskSpec\n"
            "from pathlib import Path\n"
            f"work = Path({str(work)!r})\n"
            "workspace = work / 'workspace'\n"
            "(workspace / 'src').mkdir(parents=True)\n"
            "(workspace / 'src' / 'calc.py').write_text('def add(a, b):\\n    return a + b\\n')\n"
            "audit = AuditLog(work / 'run' / 'audit' / 'journal.jsonl')\n"
            "ctx = RunContext('run-c3', TaskSpec('fix', 't', workspace), "
            "Gate({'write_file': {'path': str, 'content': str}}), "
            "Sandbox(workspace, {}), audit, ApprovalStore(audit), 10, 5, 100000)\n"
            "ctx.gate.repeat_guard.observe('write_file', {'path': 'a', 'content': '1'})\n"  # 真实观察两次同参调用
"ctx.gate.repeat_guard.observe('write_file', {'path': 'a', 'content': '1'})\n"
            "ctx.step = 4\n"
            "ctx.budget['writes_used'] = 3\n"
            "from guarded_harness.types import ConsumedCall\n"
            "ctx.consumed_calls.append(ConsumedCall('call-done', 'write_file', 'fp-2', None, 'ok'))\n"
            "from smolagents.memory import AgentMemory, ActionStep, TaskStep\n"
            "m = AgentMemory('')\n"
            "m.steps.append(TaskStep(task='demo'))\n"
            "from smolagents import Timing\n"
            "s = ActionStep(step_number=1, timing=Timing(start_time=0, end_time=0))\n"
            "s.observations = 'probe ok: before-pause'\n"
            "m.steps.append(s)\n"
            "cp = snapshot(ctx, plan='跨进程', memory_payload=[st.dict() for st in m.steps])\n"
            "CheckpointStore(work / 'run' / 'state').save(cp)\n"
            "print('SAVED', cp.seq)\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert "SAVED" in proc.stdout

        # 本进程恢复
        store = CheckpointStore(work / "run" / "state")
        cp = store.load()
        assert cp.step == 4 and cp.budget["writes_used"] == 3
        workspace = work / "workspace"
        audit = AuditLog(work / "run" / "audit" / "journal.jsonl")
        restored = RunContext(
            "run-c3", TaskSpec("fix", "t", workspace), Gate({"write_file": {"path": str, "content": str}}),
            Sandbox(workspace, {}), audit, ApprovalStore(audit), 10, 5, 100000,
        )
        apply_to_context(cp, restored)
        # 连续重复计数不清零：同指纹第 3 次仍被拦截
        rejection = restored.gate.repeat_guard.observe("write_file", {"path": "a", "content": "1"})
        assert rejection is not None and rejection.rule == "gate.repeat"
        # 已消费调用仍在：重复提交拒绝
        assert "call-done" in restored.consumed_call_ids
        # memory 可重建且包含先前观察
        memory = load_memory(cp.memory_payload)
        texts = [str(m.content) for m in memory.steps[1].to_messages()]
        assert any("probe ok: before-pause" in t for t in texts)

    def test_memory_roundtrip_preserves_task_and_final_flag(self):
        from smolagents.memory import ActionStep, AgentMemory, TaskStep

        m = AgentMemory("")
        m.steps.append(TaskStep(task="job"))
        s = ActionStep(step_number=1, timing=Timing(start_time=0, end_time=0))
        s.observations = "obs-1"
        s.is_final_answer = True
        m.steps.append(s)
        payload = dump_memory(m)
        restored = load_memory(payload)
        assert isinstance(restored.steps[0], TaskStep)
        assert restored.steps[1].observations == "obs-1"
        assert restored.steps[1].is_final_answer is True

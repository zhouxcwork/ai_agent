"""A1 审计字段测试 + 日志基础设施行为。

A2（完整任务五类事件关联）在 M4 循环可用后补入本文件。
"""

import pytest

from conftest import call_id

from guarded_harness.audit import AuditCorruptionError, AuditLog
from guarded_harness.tools import ControlledReadFile, ControlledWriteFile
from guarded_harness.types import Actor, Decision


class TestA1RejectedCallAudit:
    def test_rejected_call_has_required_fields(self, ctx):
        """A1：查看一次被拒绝的调用——rule/decision/actor/toolCallId 四字段齐且值正确。"""
        tool = ControlledReadFile(ctx)
        with call_id("call-a1"):
            observation = tool(path=12345, sanitize_inputs_outputs=True)  # 类型错误 → gate 拒绝
        assert observation.startswith("Rejected")
        records = ctx.audit.records_for_run(ctx.run_id)
        rejected = [r for r in records if r.decision == Decision.REJECT]
        assert len(rejected) == 1
        rec = rejected[0]
        assert rec.rule == "gate.param_schema"
        assert rec.decision == Decision.REJECT
        assert rec.actor == Actor.HARNESS
        assert rec.tool_call_id == "call-a1"
        assert rec.step == 0
        assert "path" in rec.detail  # 明确定位字段

    def test_sandbox_rejection_audited(self, ctx):
        tool = ControlledWriteFile(ctx)
        with call_id("call-a2"):
            tool(path="artifacts/../../.env", content="stolen\n", sanitize_inputs_outputs=True)
        rec = [r for r in ctx.audit.records_for_run(ctx.run_id) if r.decision == Decision.REJECT][-1]
        assert rec.rule == "sandbox.path_escape"
        assert rec.tool_call_id == "call-a2"

    def test_allowed_read_audited_allow_then_executed(self, ctx):
        tool = ControlledReadFile(ctx)
        with call_id("call-a3"):
            tool(path="src/calc.py", sanitize_inputs_outputs=True)
        decisions = [r.decision for r in ctx.audit.records_for_run(ctx.run_id)]
        assert Decision.ALLOW in decisions and Decision.EXECUTED in decisions


class TestAuditJournal:
    def test_append_and_read_roundtrip(self, tmp_path):
        log = AuditLog(tmp_path / "audit" / "journal.jsonl")
        from time import time

        from guarded_harness.types import AuditRecord

        first_line = log.append(
            AuditRecord(run_id="r1", step=1, tool_call_id="c1", rule="x", decision=Decision.ALLOW,
                        actor=Actor.MODEL, detail="d", timestamp=time())
        )
        log.append(
            AuditRecord(run_id="r2", step=2, tool_call_id="c2", rule="y", decision=Decision.STOP,
                        actor=Actor.HARNESS, detail="d2", timestamp=time())
        )
        assert first_line == 1
        assert log.count() == 2
        records = log.read_all()
        assert [r.run_id for _, r in records] == ["r1", "r2"]
        assert log.tail_for_run("r2", after_line=1)[0][1].rule == "y"
        assert log.tail_for_run("r1", after_line=0)[0][0] == 1

    def test_torn_tail_raises_in_strict_mode(self, tmp_path):
        log = AuditLog(tmp_path / "journal.jsonl")
        path = tmp_path / "journal.jsonl"
        path.write_text('{"half": "wr', encoding="utf-8")  # 半写：无换行、非完整 JSON
        with pytest.raises(AuditCorruptionError):
            log.read_all(strict=True)
        # 非严格读取丢弃损坏尾行（诊断用途），不静默当作有效记录
        assert log.read_all() == []

    def test_corrupt_middle_line_strict_raises(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        path.write_text('{"ok": 1}\nnot-json\n', encoding="utf-8")
        log = AuditLog(path)
        with pytest.raises(AuditCorruptionError):
            log.read_all(strict=True)

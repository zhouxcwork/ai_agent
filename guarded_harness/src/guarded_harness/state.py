"""检查点与恢复（C1-C4，PLAN §5.2/§6.2/§6.2.1/§6.3）。

- CheckpointStore：同目录临时文件 + fsync + 原子替换；checksum 检损坏，损坏明确拒绝。
- snapshot/apply：RunContext（工具层运行态）与 Checkpoint（持久信封）互转。
- dump_memory/load_memory：AgentMemory 步骤 JSON 往返（M0a E6 验证的方式）。
- align_recovery：按 journal_cursor 对齐执行日志——悬挂 INTENT → UNKNOWN_RESULT，
  结果已落盘但存档旧、日志损坏均保守 NEEDS_REVIEW，不重放、不自动重建。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .audit import AuditCorruptionError, AuditLog
from .gate import RepeatGuard
from .sandbox import Sandbox
from .tools import RunContext, TaskSpec
from .types import Checkpoint, Decision, ProposalStatus, StopReason, checkpoint_from_dict
from smolagents import AgentError, AgentLogger
from smolagents.memory import ActionStep, AgentMemory, TaskStep

CHECKPOINT_FILE = "checkpoint.json"


class CheckpointCorruptedError(Exception):
    """存档 checksum/schema 不符或半写损坏：拒绝加载，不静默回退旧档重放。"""


class CheckpointStore:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.state_dir / CHECKPOINT_FILE

    def save(self, cp: Checkpoint) -> Checkpoint:
        """落盘前封口：seq 自增、checksum 覆盖当前内容；原子替换避免半写。"""
        previous = self.load() if self.path.exists() else None
        cp.seq = max(cp.seq, previous.seq if previous else 0) + 1
        cp.checksum = cp.compute_checksum()
        data = cp.payload_dict()
        data["checksum"] = cp.checksum
        tmp = self.state_dir / f"{CHECKPOINT_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        return cp

    def load(self) -> Checkpoint:
        """加载并校验信封。checksum 不符/schema 不识别/JSON 损坏一律抛错。"""
        if not self.path.exists():
            raise FileNotFoundError(f"no checkpoint at {self.path}")
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)  # JSONDecodeError → 视为损坏，由调用方转 NEEDS_REVIEW
        if data.get("schema_version") != 1:
            raise CheckpointCorruptedError(f"unsupported schema_version {data.get('schema_version')!r}")
        checksum = data.pop("checksum", "")
        payload = dict(data)
        # 还原枚举/嵌套结构后重算 checksum（与保存侧同构）
        twin = checkpoint_from_dict(payload)
        if twin.compute_checksum() != checksum:
            raise CheckpointCorruptedError("checkpoint checksum mismatch")
        cp = checkpoint_from_dict(payload)
        cp.checksum = checksum
        return cp


# ---------------------------------------------------------------- RunContext ↔ Checkpoint

def snapshot(ctx: RunContext, *, plan: str = "", current_call_id: str = "",
             stop_reason: StopReason | None = None, memory_payload=None) -> Checkpoint:
    """从运行态构造检查点（内容即当前真相；由 loop 在安全步边界调用并存档）。"""
    return Checkpoint(
        run_id=ctx.run_id,
        task={
            "input": ctx.task.input_text,
            "mode": ctx.task.mode,
            "artifact_target": ctx.task.artifact_target,
            "workspace_root": str(ctx.task.workspace_root),
        },
        status=ctx.state,
        stop_reason=stop_reason,
        repeat_guard=dict(ctx.gate.repeat_guard.state()),
        consumed_calls=list(ctx.consumed_calls),
        journal_cursor=ctx.audit.count(),
        current_call_id=current_call_id,
        step=ctx.step,
        plan=plan,
        evidence=list(ctx.evidence),
        budget=dict(ctx.budget),
        pending_approvals=[p for p in ctx.approvals.proposals.values()
                           if p.status in (ProposalStatus.PENDING, ProposalStatus.APPROVED)],
        memory_payload=memory_payload,
        workspace_digest=ctx.sandbox.digest_fileset(ctx.task.digest_patterns, ctx.task.digest_excludes),
    )


def apply_to_context(cp: Checkpoint, ctx: RunContext) -> None:
    """检查点还原到运行态：状态/步数/重复计数/消费记录/预算/证据/待审批。"""
    ctx.state = cp.status
    ctx.step = cp.step
    ctx.gate.repeat_guard = RepeatGuard(**cp.repeat_guard)
    ctx.consumed_calls = list(cp.consumed_calls)
    ctx.budget = dict(cp.budget)
    ctx.evidence = list(cp.evidence)
    ctx.approvals.proposals = {p.proposal_id: p for p in cp.pending_approvals}


# ---------------------------------------------------------------- AgentMemory 序列化

def dump_memory(memory: AgentMemory) -> list[dict]:
    """步骤 dict 列表（M0a E6：JSON 可往返）。"""
    return [step.dict() for step in memory.steps]


def load_memory(payload: list[dict]) -> AgentMemory:
    """从 payload 重建 AgentMemory。只还原驱动对话所需字段（to_messages 的输入）。"""
    from smolagents import Timing

    memory = AgentMemory(system_prompt="")
    for d in payload:
        if "task" in d:
            memory.steps.append(TaskStep(task=d["task"]))
            continue
        # timing 仅作展示用途；占位 0/0 以保证 step.dict() 可再序列化
        step = ActionStep(step_number=d.get("step_number", 0), timing=Timing(start_time=0, end_time=0))
        step.model_output = d.get("model_output")
        step.observations = d.get("observations")
        step.is_final_answer = bool(d.get("is_final_answer", False))
        if d.get("error"):
            step.error = AgentError(d["error"]["message"], AgentLogger())
        memory.steps.append(step)
    return memory


# ---------------------------------------------------------------- 恢复对齐（§6.2.1）

@dataclass
class Alignment:
    """日志对齐结论：loop 据此决定恢复或停止核对。"""

    new_cursor: int = 0
    block_reason: str = ""  # 非空 → 保守停止（NEEDS_REVIEW），不得自动重放/重建
    unknown_result_calls: list[str] = field(default_factory=list)  # 悬挂 INTENT 的 tool_call_id
    decided_proposal_ids: dict[str, str] = field(default_factory=dict)  # proposal_id → approved|rejected


def align_recovery(cp: Checkpoint, audit: AuditLog) -> Alignment:
    """读取 cursor 之后的运行日志并裁决恢复安全性（C2）。"""
    try:
        tail = audit.tail_for_run(cp.run_id, cp.journal_cursor)
    except AuditCorruptionError as exc:
        return Alignment(block_reason=f"journal corrupt: {exc}")
    result = Alignment(new_cursor=audit.count())
    intents: dict[str, AuditRecord] = {}
    executed: set[str] = set()
    for _, rec in tail:
        if rec.rule == "approval.execute_intent" and rec.decision == Decision.INTENT:
            intents[rec.tool_call_id] = rec
        elif rec.decision == Decision.EXECUTED and rec.rule == "write.result":
            executed.add(rec.tool_call_id)
        elif rec.rule == "approval.approved":
            result.decided_proposal_ids[_pid(rec.detail)] = "approved"
        elif rec.rule == "approval.rejected":
            result.decided_proposal_ids[_pid(rec.detail)] = "rejected"
    result.unknown_result_calls = sorted(set(intents) - executed)
    if result.unknown_result_calls:
        result.block_reason = (
            "dangling execution INTENT without persisted result: " + ", ".join(result.unknown_result_calls)
        )
    elif intents and executed:  # 结果已落盘但 checkpoint 未更新：保守停止，不自动重建
        result.block_reason = "execution results persisted after checkpoint; manual review required"
    return result


def workspace_changed(cp: Checkpoint, sandbox: Sandbox, task: TaskSpec) -> bool:
    """C4：重算约定文件集合指纹并与存档比对。"""
    return sandbox.digest_fileset(task.digest_patterns, task.digest_excludes) != cp.workspace_digest


def _pid(detail: str) -> str:
    for part in detail.split():
        if part.startswith("proposal_id="):
            return part.split("=", 1)[1]
    return ""

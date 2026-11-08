"""协议词汇与核心数据结构（PLAN §5）。

RunState/StopReason/Decision/Actor 为状态机词汇；Evidence/AuditRecord/Proposal/Checkpoint
为跨进程持久化的数据信封，全部可 JSON 往返。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any


class RunState(str, Enum):
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StopReason(str, Enum):
    COMPLETED = "COMPLETED"
    MAX_TURNS = "MAX_TURNS"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    INTERRUPTED = "INTERRUPTED"
    PAUSED_BY_USER = "PAUSED_BY_USER"
    MODEL_ERROR = "MODEL_ERROR"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"


class Decision(str, Enum):
    ALLOW = "ALLOW"
    REJECT = "REJECT"
    WAIT = "WAIT"
    INTENT = "INTENT"
    EXECUTED = "EXECUTED"
    RESUME = "RESUME"
    CHECKPOINT = "CHECKPOINT"
    STOP = "STOP"


class Actor(str, Enum):
    MODEL = "model"
    HARNESS = "harness"
    USER = "user"


class ProposalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CONSUMED = "CONSUMED"


def _jsonable(obj: Any) -> Any:
    """递归转换为可 JSON 序列化的原生类型（dataclass/Enum/Path）。"""
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return str(obj) if not isinstance(obj, (str, int, float, bool, type(None))) else obj


def canonical_json(obj: Any) -> str:
    return json.dumps(_jsonable(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Evidence:
    """测试/产物/读取证据（PLAN §5.3）。content_digest 绑受测内容指纹，实现 H2 证据失效。"""

    kind: str  # "test_run" | "artifact" | "source_read"
    scope: str
    command_id: str | None = None
    exit_code: int | None = None
    passed: bool = False
    content_digest: str | None = None
    output_summary: str = ""
    tool_call_id: str = ""
    timestamp: float = 0.0


@dataclass
class AuditRecord:
    """审计记录（PLAN §5.4）。A1 验收线：rule/decision/actor/tool_call_id。"""

    run_id: str
    step: int
    tool_call_id: str
    rule: str
    decision: Decision
    actor: Actor
    detail: str = ""
    timestamp: float = 0.0


@dataclass
class Proposal:
    """待审批写操作提案：绑定运行、调用、规范化参数与文件现场（PLAN §6.1）。"""

    proposal_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, str]
    target_realpath: str
    file_state_digest: str  # 提案时目标文件内容指纹（不存在为空串）
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: float = 0.0
    decided_by: str = ""
    decided_at: float | None = None


@dataclass
class ConsumedCall:
    """已消费的调用：跨进程拒绝重复执行同一调用/重复批准（G4）。"""

    tool_call_id: str
    tool_name: str
    arguments_fingerprint: str
    proposal_id: str | None = None
    result_status: str = ""  # ok | failed | unknown（执行中） | UNKNOWN_RESULT（恢复确认结果不明）


@dataclass
class Checkpoint:
    """检查点信封（PLAN §5.2）。checksum 校验内容完整性，原子替换防半写。"""

    schema_version: int = 1
    run_id: str = ""
    seq: int = 0
    task: dict[str, str] = field(default_factory=dict)  # input/mode/artifact_target/workspace_root
    status: RunState = RunState.RUNNING
    stop_reason: StopReason | None = None
    repeat_guard: dict[str, Any] = field(default_factory=lambda: {"last_fingerprint": None, "consecutive_count": 0})
    consumed_calls: list[ConsumedCall] = field(default_factory=list)
    journal_cursor: int = 0
    current_call_id: str = ""
    step: int = 0
    plan: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    budget: dict[str, int] = field(
        default_factory=lambda: {
            "turns_used": 0,
            "writes_used": 0,
            "tokens_used": 0,
            "max_turns": 0,
            "max_writes": 0,
            "max_tokens": 0,
        }
    )
    pending_approvals: list[Proposal] = field(default_factory=list)
    memory_payload: Any = None
    workspace_digest: str = ""

    def payload_dict(self) -> dict[str, Any]:
        """不含 checksum 的负载（checksum 的计算对象）。"""
        d = _jsonable(self)
        d.pop("checksum", None)
        return d

    def compute_checksum(self) -> str:
        return sha256_text(canonical_json(self.payload_dict()))


def checkpoint_from_dict(d: dict[str, Any]) -> Checkpoint:
    """从字典重建 Checkpoint（枚举与嵌套 dataclass 还原）。checksum 字段单独校验。"""
    known = {f.name for f in fields(Checkpoint)}
    cp = Checkpoint(**{k: v for k, v in d.items() if k in known and k != "checksum"})
    cp.status = RunState(cp.status)
    if cp.stop_reason is not None:
        cp.stop_reason = StopReason(cp.stop_reason)
    cp.evidence = [Evidence(**e) for e in cp.evidence]
    cp.consumed_calls = [ConsumedCall(**c) for c in cp.consumed_calls]
    cp.pending_approvals = [
        Proposal(status=ProposalStatus(p["status"]), **{k: v for k, v in p.items() if k != "status"})
        for p in cp.pending_approvals
    ]
    return cp


def proposal_from_dict(d: dict[str, Any]) -> Proposal:
    return Proposal(status=ProposalStatus(d["status"]), **{k: v for k, v in d.items() if k != "status"})

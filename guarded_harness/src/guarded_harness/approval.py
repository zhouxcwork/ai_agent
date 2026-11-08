"""审批（G3/G4）：提案登记与人工决定，跨进程持久化经审计日志。

提案对象由 checkpoint 携带；决定的持久真相是审计日志（决定即落盘）。
执行不在本模块：工程侧代执行由 loop.py 在消费提案时完成（两阶段审计，
INTENT 兼作消费凭据，见 PLAN §6.1）。
"""

from __future__ import annotations

import uuid
from time import time

from .audit import AuditLog
from .types import Actor, AuditRecord, Decision, Proposal, ProposalStatus


class ApprovalError(Exception):
    """提案状态不允许该操作（重复批准/重复消费/未知提案）。"""


class ApprovalStore:
    def __init__(self, audit: AuditLog, proposals: dict[str, Proposal] | None = None):
        self.audit = audit
        self.proposals: dict[str, Proposal] = proposals if proposals is not None else {}

    def register(self, run_id: str, step: int, tool_call_id: str, tool_name: str,
                 arguments: dict[str, str], target_realpath: str, file_state_digest: str) -> Proposal:
        proposal = Proposal(
            proposal_id=f"prop-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            target_realpath=target_realpath,
            file_state_digest=file_state_digest,
            status=ProposalStatus.PENDING,
            created_at=time(),
        )
        self.proposals[proposal.proposal_id] = proposal
        self.audit.append(
            AuditRecord(
                run_id=run_id,
                step=step,
                tool_call_id=tool_call_id,
                rule="approval.pending",
                decision=Decision.WAIT,
                actor=Actor.MODEL,
                detail=f"proposal_id={proposal.proposal_id} target={target_realpath} "
                f"args_fingerprint={_args_fingerprint(tool_name, arguments)}",
                timestamp=time(),
            )
        )
        return proposal

    def get(self, proposal_id: str) -> Proposal:
        if proposal_id not in self.proposals:
            raise ApprovalError(f"unknown proposal '{proposal_id}'")
        return self.proposals[proposal_id]

    def decide(self, run_id: str, proposal_id: str, approved: bool, *, by: str = "user", step: int = 0) -> Proposal:
        """记录人工决定（持久化到日志）。仅接受 PENDING 提案——重复批准直接拒绝（G4）。"""
        proposal = self.get(proposal_id)
        if proposal.run_id != run_id:
            raise ApprovalError(f"proposal '{proposal_id}' belongs to run '{proposal.run_id}'")
        if proposal.status != ProposalStatus.PENDING:
            raise ApprovalError(
                f"proposal '{proposal_id}' is already {proposal.status.value}; duplicate decisions are rejected"
            )
        proposal.status = ProposalStatus.APPROVED if approved else ProposalStatus.REJECTED
        proposal.decided_by = by
        proposal.decided_at = time()
        self.audit.append(
            AuditRecord(
                run_id=run_id,
                step=step,
                tool_call_id=proposal.tool_call_id,
                rule="approval.approved" if approved else "approval.rejected",
                decision=Decision.ALLOW if approved else Decision.REJECT,
                actor=Actor.USER,
                detail=f"proposal_id={proposal_id} by={by}",
                timestamp=time(),
            )
        )
        return proposal

    def consume_intent(self, run_id: str, proposal_id: str, step: int) -> Proposal:
        """持久化单条执行 INTENT（兼作消费凭据）：落盘成功后提案才算消费，才允许执行 handler。

        记录包含 proposal_id、调用/参数指纹与现场指纹；预算预留由调用方写入 detail。
        未完整持久化成功则不执行 handler（调用方负责：先本方法、后 handler）。
        """
        proposal = self.get(proposal_id)
        if proposal.status != ProposalStatus.APPROVED:
            raise ApprovalError(
                f"proposal '{proposal_id}' is {proposal.status.value}, only APPROVED can be consumed"
            )
        proposal.status = ProposalStatus.CONSUMED
        self.audit.append(
            AuditRecord(
                run_id=run_id,
                step=step,
                tool_call_id=proposal.tool_call_id,
                rule="approval.execute_intent",
                decision=Decision.INTENT,
                actor=Actor.HARNESS,
                detail=(
                    f"proposal_id={proposal_id} call={proposal.tool_call_id} "
                    f"args_fingerprint={_args_fingerprint(proposal.tool_name, proposal.arguments)} "
                    f"file_state_digest={proposal.file_state_digest}"
                ),
                timestamp=time(),
            )
        )
        return proposal


def _args_fingerprint(tool_name: str, arguments: dict) -> str:
    from .gate import fingerprint

    return fingerprint(tool_name, arguments)

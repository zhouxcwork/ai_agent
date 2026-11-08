"""HarnessLoop（循环层，PLAN §3/§6）：步进驱动、审批暂停→工程侧代执行→恢复、
预算与 StopReason、跨进程恢复编排。

接入方式（D11）：子类化 ToolCallingAgent 重写 process_tool_calls（整批串行；
暂停后剩余调用只回观察不执行）与 execute_tool_call（ContextVar 传 toolCallId）；
循环层消费 run(stream=True) 到 ActionStep 步边界，暂停即停止拉取。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from time import time

from smolagents import (
    ActionStep,
    AgentError,
    AgentGenerationError,
    AgentMaxStepsError,
    ToolCallingAgent,
    ToolCall,
    ToolOutput,
    Timing,
)

from smolagents.utils import AgentToolCallError

from .approval import ApprovalError, ApprovalStore
from .audit import AuditLog
from .completion import make_final_answer_check
from .gate import Gate, fingerprint
from .sandbox import Sandbox
from .state import CheckpointStore, align_recovery, apply_to_context, dump_memory, load_memory, snapshot, workspace_changed
from .tools import RunContext, TaskSpec, current_tool_call_id, make_tools, validate_write_target
from .types import Actor, ConsumedCall, Decision, Evidence, ProposalStatus, RunState, StopReason

TASK_HINTS = (
    "\nYou are working inside a guarded harness. Notes:\n"
    "- All paths are relative to the workspace root; writes require human approval and are executed by the system.\n"
    "- Use run_test to verify; a final answer is only accepted if the program-side completion check passes.\n"
    "- Never claim success without fresh passing test evidence."
)


class ResumeRefused(Exception):
    """恢复请求被拒绝（终态/存档损坏等）。"""


@dataclass
class RunOutcome:
    state: RunState
    stop_reason: StopReason | None
    step: int
    budget: dict
    pending_proposals: list[str]

    def summary(self) -> str:
        return (
            f"state={self.state.value} stop_reason={self.stop_reason.value if self.stop_reason else None} "
            f"step={self.step} budget={self.budget} pending={self.pending_proposals}"
        )


class SerialAgent(ToolCallingAgent):
    """公开接口扩展：整批工具调用串行执行；暂停后剩余调用只回观察、不执行 handler。"""

    def __init__(self, *args, ctx: RunContext, **kwargs):
        super().__init__(*args, **kwargs)
        self.ctx = ctx

    def execute_tool_call(self, tool_name, arguments):
        token = current_tool_call_id.set(self._pending_tool_call_id)
        try:
            if tool_name not in {**self.tools, **self.managed_agents}:
                self.ctx.record("gate.unknown_tool", Decision.REJECT, Actor.HARNESS,
                                f"Unknown tool: {tool_name}")
            try:
                return super().execute_tool_call(tool_name, arguments)
            except AgentToolCallError as exc:
                # 框架会在 forward 前验证参数；此入口补齐该拒绝路径的调用级审计。
                self.ctx.record("gate.param_schema", Decision.REJECT, Actor.HARNESS, str(exc))
                raise
        finally:
            current_tool_call_id.reset(token)

    def process_tool_calls(self, chat_message, memory_step):
        assert chat_message.tool_calls is not None
        outputs: dict[str, ToolOutput] = {}
        ordered: list[ToolCall] = []
        for chat_tool_call in chat_message.tool_calls:
            ordered.append(
                ToolCall(
                    name=chat_tool_call.function.name,
                    arguments=chat_tool_call.function.arguments,
                    id=chat_tool_call.id,
                )
            )

        def make_output(tc: ToolCall, result) -> ToolOutput:
            return ToolOutput(
                id=tc.id,
                output=result,
                is_final_answer=(tc.name == "final_answer" and self.ctx.state == RunState.RUNNING),
                observation=str(result).strip(),
                tool_call=tc,
            )

        for tc in ordered:
            if self.ctx.audit.failed:
                self.ctx.state = RunState.FAILED
            if self.ctx.state != RunState.RUNNING:
                # 暂停/等待审批后：本批剩余调用不产生任何副作用
                out = make_output(tc, "SKIPPED: run paused (approval or review pending); no side effects")
            else:
                self._pending_tool_call_id = tc.id
                out = make_output(tc, self.execute_tool_call(tc.name, tc.arguments or {}))
            outputs[tc.id] = out
            # 消费方可能在任意 yield 后停止：memory 字段随每个 yield 增量更新
            memory_step.tool_calls = ordered[: ordered.index(tc) + 1]
            memory_step.observations = "\n".join(outputs[k].observation for k in sorted(outputs))
            yield out


class HarnessLoop:
    def __init__(self, ctx: RunContext, model, store: CheckpointStore, extra_instructions: str = ""):
        self.ctx = ctx
        self.store = store
        self.model = model
        self.agent = SerialAgent(
            tools=make_tools(ctx),
            model=model,
            ctx=ctx,
            max_steps=max(ctx.budget["max_turns"] + 2, 20),  # 框架上限只作保险，预算由外层先停
        )
        self.agent.final_answer_checks = [make_final_answer_check(ctx)]
        self.task_text = ctx.task.input_text + TASK_HINTS + extra_instructions
        self._gen = None
        self._started = False  # 是否已启动过（决定 reset 与重复 TaskStep 处理）
        self.plan = ("读取相关源码 → 提出并审批修改 → 执行配置测试 → 生成交付说明 → 程序验收"
                     if ctx.task.mode == "fix" else "读取相关源码 → 生成并审批说明文档 → 程序验收")
        self._stop_reason: StopReason | None = None
        self._driving = False

    # ------------------------------------------------------------ 对外：驱动与控制

    def run(self) -> RunOutcome:
        """新任务或继续驱动，直到安全边界（完成/等待审批/暂停/停止）。"""
        self._ensure_generator(reset=not self._started)
        return self._drive()

    def resume(self) -> RunOutcome:
        """恢复：执行已批准未消费提案（工程侧代执行），再驱动模型。"""
        if self.ctx.state in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED):
            raise ResumeRefused(f"run is terminal ({self.ctx.state.value}); refusing resume")
        if self.ctx.state == RunState.PAUSED and self._stop_reason == StopReason.NEEDS_REVIEW:
            # 等待人工核对：自动恢复被拒，本作业不提供强制绕过入口
            return self._outcome()
        if self.ctx.state == RunState.PAUSED and self._stop_reason in (
            StopReason.MAX_TURNS, StopReason.BUDGET_EXHAUSTED
        ):
            return self._outcome()  # 预算尽不自动提升，也不续跑
        executed_any = False
        for proposal_id in [p.proposal_id for p in self.ctx.approvals.proposals.values()
                            if p.status == ProposalStatus.APPROVED]:
            if self.ctx.state == RunState.PAUSED:  # 执行中转入 NEEDS_REVIEW
                break
            executed_any |= self._execute_approved(proposal_id)
        if self.ctx.state == RunState.PAUSED and self._stop_reason == StopReason.NEEDS_REVIEW:
            return self._outcome()  # 代执行重校验失败，已转入人工核对
        if self.ctx.state == RunState.WAITING_APPROVAL and not executed_any:
            # 待审批且无决定：不启动模型（进程可退出）
            self._save(None)
            return self._outcome()
        self.ctx.state = RunState.RUNNING
        return self.run()

    def approve(self, proposal_id: str, by: str = "user") -> None:
        """仅记录人工决定；执行在 resume()（跨进程/进程内同一入口）。"""
        self.ctx.approvals.decide(self.ctx.run_id, proposal_id, approved=True, by=by, step=self.ctx.step)

    def reject(self, proposal_id: str, by: str = "user") -> None:
        proposal = self.ctx.approvals.decide(self.ctx.run_id, proposal_id, approved=False, by=by, step=self.ctx.step)
        self._append_observation_step(
            f"REJECTED proposal {proposal.proposal_id} for '{proposal.arguments.get('path')}': "
            "the human rejected this write. Change approach; do not resubmit identical content."
        )
        if self.ctx.state == RunState.WAITING_APPROVAL:
            self.ctx.state = RunState.RUNNING
        self._save(None)

    def cancel(self) -> RunOutcome:
        """用户取消：终止在途测试进程组；运行进入 CANCELLED（终态，不可续跑）。

        驱动线程在下一个步边界看到 cancel_event 自行收尾；此处仅当不在驱动中时
        立即定终态（非驱动态不存在在途模型请求或测试进程）。
        """
        self.ctx.cancel_event.set()
        self.agent.interrupt()
        if self._driving:
            return self._outcome()  # 由驱动线程在边界收尾
        self.ctx.state = RunState.CANCELLED
        self._close_generator()
        self._save(StopReason.INTERRUPTED)
        self.ctx.record("run.cancelled", Decision.STOP, Actor.USER, "user cancelled")
        return self._outcome()

    # ------------------------------------------------------------ 步进驱动

    def _ensure_generator(self, reset: bool) -> None:
        if self._gen is not None:
            return
        self._gen = self.agent.run(self.task_text, stream=True, reset=reset)
        self._started = True
        if not reset:
            # run() 每次都会追加一条 TaskStep；恢复时去掉重复（M0a E6 记录的现象）
            from smolagents.memory import TaskStep

            steps = self.agent.memory.steps
            task_steps = [i for i, s in enumerate(steps) if isinstance(s, TaskStep)]
            if len(task_steps) > 1:
                steps.pop(task_steps[-1])

    def _consume_step(self):
        """推进到下一个 ActionStep（步边界）。返回该步；生成器耗尽返回 None。"""
        from smolagents.agents import ActionOutput

        for out in self._gen:
            if isinstance(out, ActionStep):
                return out
        return None

    def _close_generator(self) -> None:
        if self._gen is not None:
            self._gen.close()
            self._gen = None

    def _drive(self) -> RunOutcome:
        ctx = self.ctx
        self._driving = True
        try:
            return self._drive_inner()
        finally:
            self._driving = False

    def _drive_inner(self) -> RunOutcome:
        ctx = self.ctx
        while True:
            # —— 边界检查（均在下一次模型请求之前）——
            if ctx.audit.failed:
                ctx.state = RunState.FAILED
                self._stop_reason = StopReason.PERSISTENCE_FAILED
                self._close_generator()
                return self._outcome()
            if ctx.cancel_event.is_set():
                ctx.state = RunState.CANCELLED
                return self._finish(StopReason.INTERRUPTED)
            if ctx.state in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED):
                return self._outcome()
            if ctx.state == RunState.PAUSED:
                return self._outcome()
            if ctx.state == RunState.WAITING_APPROVAL:
                return self._outcome()  # 挂起中不允许直接驱动；必须经 resume()/reject() 解除
            if ctx.budget["turns_used"] >= ctx.budget["max_turns"]:
                return self._finish(StopReason.MAX_TURNS, state=RunState.PAUSED)
            if ctx.budget["tokens_used"] >= ctx.budget["max_tokens"]:
                return self._finish(StopReason.BUDGET_EXHAUSTED, state=RunState.PAUSED)
            ctx.write_budget_refused = False
            # —— 消费一个 Harness 步 ——
            try:
                step = self._consume_step()
            except AgentGenerationError:
                # 模型请求失败：可恢复暂停，预算不清零
                return self._finish(StopReason.MODEL_ERROR, state=RunState.PAUSED)
            except AgentError as exc:
                if "interrupted" in str(exc):
                    return self._finish(StopReason.INTERRUPTED, state=RunState.CANCELLED)
                return self._finish(StopReason.MODEL_ERROR, state=RunState.PAUSED)
            if step is None:
                # 生成器耗尽而无完成步：外层预算先行停止的兜底保护不应到达这里
                return self._finish(StopReason.MAX_TURNS, state=RunState.PAUSED)
            # —— 步后处理 ——
            if ctx.audit.failed:
                ctx.state = RunState.FAILED
                self._stop_reason = StopReason.PERSISTENCE_FAILED
                self._close_generator()
                return self._outcome()
            ctx.step += 1
            ctx.budget["turns_used"] += 1
            if step.token_usage is not None:
                ctx.budget["tokens_used"] += step.token_usage.input_tokens + step.token_usage.output_tokens
            # 响应产生后才知道实际 Token 用量，必须先裁决超限再接受完成。
            if ctx.budget["tokens_used"] >= ctx.budget["max_tokens"]:
                return self._finish(StopReason.BUDGET_EXHAUSTED, state=RunState.PAUSED)
            if step.is_final_answer and step.error is None:
                # 确认边界：final_answer_checks 已通过（M0a E4 时序）
                return self._finish(StopReason.COMPLETED, state=RunState.COMPLETED)
            if ctx.state == RunState.WAITING_APPROVAL:
                # 挂起边界：持久化提案与状态后停止消费，无进一步模型请求
                self._save(None)
                return self._outcome()
            if isinstance(step.error, AgentMaxStepsError):
                return self._finish(StopReason.MAX_TURNS, state=RunState.PAUSED)
            if ctx.write_budget_refused and not step.is_final_answer:
                return self._finish(StopReason.BUDGET_EXHAUSTED, state=RunState.PAUSED)
            self._save(None)  # C1：每个安全步骤后保存检查点

    # ------------------------------------------------------------ 审批代执行（§6.1）

    def _execute_approved(self, proposal_id: str) -> bool:
        """工程侧代执行已批准提案：重校验 → INTENT（消费凭据）→ handler → 结果落盘。"""
        ctx = self.ctx
        proposal = ctx.approvals.get(proposal_id)
        if proposal.run_id != ctx.run_id or proposal.tool_call_id in ctx.consumed_call_ids:
            ctx.record("approval.duplicate_execution", Decision.REJECT, Actor.HARNESS,
                       f"proposal {proposal_id} already consumed", tool_call_id=proposal.tool_call_id)
            return False
        # 1) 重校验：参数再过 gate/schema 与路径规则（与首次提交同一校验链）
        real, srej = validate_write_target(ctx, proposal.arguments["path"])
        if srej is not None:
            ctx.record("approval.revalidation_failed", Decision.REJECT, Actor.HARNESS,
                       f"{srej.rule}: {srej.message}", tool_call_id=proposal.tool_call_id)
            self._needs_review(f"write target revalidation failed: {srej.rule}")
            return False
        if str(real) != proposal.target_realpath:
            ctx.record("approval.stale_target", Decision.REJECT, Actor.HARNESS,
                       f"realpath changed: {proposal.target_realpath} -> {real}", tool_call_id=proposal.tool_call_id)
            self._needs_review("proposal target realpath changed since registration")
            return False
        existing_digest = hashlib.sha256(real.read_bytes() if real.exists() else b"").hexdigest()
        if existing_digest != proposal.file_state_digest:
            ctx.record("approval.stale_state", Decision.REJECT, Actor.HARNESS,
                       "target file state changed since proposal", tool_call_id=proposal.tool_call_id)
            self._needs_review("proposal file state changed since registration")
            return False
        if ctx.writes_exhausted():
            ctx.record("approval.budget_refused", Decision.REJECT, Actor.HARNESS,
                       "write budget exhausted before execution", tool_call_id=proposal.tool_call_id)
            self._needs_review("write budget exhausted before approved execution")
            return False
        # 2) 执行 INTENT：先落盘消费凭据，后执行 handler（C2 崩溃窗口由此可判）
        ctx.approvals.consume_intent(ctx.run_id, proposal_id, step=ctx.step)
        consumed = ConsumedCall(
            tool_call_id=proposal.tool_call_id,
            tool_name=proposal.tool_name,
            arguments_fingerprint=fingerprint(proposal.tool_name, proposal.arguments),
            proposal_id=proposal_id,
            result_status="unknown",  # 结果未落盘前一律 unknown
        )
        ctx.consumed_calls.append(consumed)
        # 3) handler 执行（原子写）
        ok, detail = self._write_file(real, proposal.arguments["content"])
        # 4) 结果落盘（崩溃在本行之前 → 恢复对齐为 UNKNOWN_RESULT，不自动重放）
        consumed.result_status = "ok" if ok else "failed"
        ctx.record("write.result", Decision.EXECUTED, Actor.HARNESS,
                   f"proposal={proposal_id} {'ok' if ok else 'failed'}: {detail}",
                   tool_call_id=proposal.tool_call_id)
        ctx.budget["writes_used"] += 1
        ctx.evidence.append(
            Evidence(kind="artifact", scope=proposal.arguments["path"], passed=ok,
                     tool_call_id=proposal.tool_call_id, timestamp=time())
        )
        # 5) memory observation：模型下一轮可见执行结果（不依赖模型重放）
        self._append_observation_step(
            f"APPROVED_WRITE executed: '{proposal.arguments['path']}' {'ok' if ok else 'FAILED: ' + detail}"
        )
        return ok

    @staticmethod
    def _write_file(real: Path, content: str) -> tuple[bool, str]:
        try:
            real.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".{real.name}.", suffix=".tmp", dir=real.parent)
            tmp = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(content)
                    fh.flush()
                    os.fsync(fh.fileno())
                tmp.replace(real)
            finally:
                tmp.unlink(missing_ok=True)
            return True, f"{len(content)} chars written"
        except OSError as exc:
            return False, str(exc)

    def _append_observation_step(self, text: str) -> None:
        step = ActionStep(step_number=self.ctx.step + 1, timing=Timing(start_time=time(), end_time=time()))
        step.observations = text
        self.agent.memory.steps.append(step)

    # ------------------------------------------------------------ 存档与状态迁移

    def _save(self, stop_reason: StopReason | None) -> None:
        try:
            cp = snapshot(
                self.ctx,
                plan=self.plan,
                current_call_id=current_tool_call_id.get(),
                stop_reason=stop_reason,
                memory_payload=dump_memory(self.agent.memory),
            )
            self.store.save(cp)
        except OSError as exc:
            # 关键存档失败：当前进程停止推进（PERSISTENCE_FAILED），不静默继续
            self.ctx.state = RunState.FAILED
            self._stop_reason = StopReason.PERSISTENCE_FAILED
            self.ctx.record("run.persistence_failed", Decision.STOP, Actor.HARNESS, str(exc))
            raise
        if stop_reason is not None:
            self.ctx.record("run.stopped", Decision.STOP, Actor.HARNESS, stop_reason.value)
            if stop_reason in (StopReason.COMPLETED, StopReason.PERSISTENCE_FAILED):
                pass  # 状态已由 _finish 设置
            elif stop_reason == StopReason.INTERRUPTED:
                self.ctx.state = RunState.CANCELLED
            else:
                self.ctx.state = RunState.PAUSED
            self._stop_reason = stop_reason

    def _finish(self, stop_reason: StopReason, state: RunState | None = None) -> RunOutcome:
        if state is not None:
            self.ctx.state = state
        self._close_generator()
        self._save(stop_reason)
        return self._outcome()

    def _needs_review(self, reason: str) -> None:
        self.ctx.state = RunState.PAUSED
        self._save(StopReason.NEEDS_REVIEW)
        self._close_generator()

    def _outcome(self) -> RunOutcome:
        pending = [p.proposal_id for p in self.ctx.approvals.proposals.values()
                   if p.status == ProposalStatus.PENDING]
        return RunOutcome(
            state=self.ctx.state,
            stop_reason=self._stop_reason,
            step=self.ctx.step,
            budget=dict(self.ctx.budget),
            pending_proposals=pending,
        )


# ------------------------------------------------------------ 跨进程恢复编排（§6.3）

def build_context(run_id: str, task: TaskSpec, run_dir: Path, gate: Gate, sandbox: Sandbox,
                  max_turns: int, max_writes: int, max_tokens: int) -> RunContext:
    audit = AuditLog(run_dir / "audit" / "journal.jsonl")
    return RunContext(
        run_id=run_id,
        task=task,
        gate=gate,
        sandbox=sandbox,
        audit=audit,
        approvals=ApprovalStore(audit),
        max_turns=max_turns,
        max_writes=max_writes,
        max_tokens=max_tokens,
    )


def load_run(run_dir: Path, model, *, gate: Gate, sandbox: Sandbox, task: TaskSpec) -> HarnessLoop:
    """跨进程恢复入口：加载存档 → 终态拒恢复 → 日志对齐 → 工作区指纹 → 还原运行态。"""
    from .state import CheckpointCorruptedError

    store = CheckpointStore(run_dir / "state")
    try:
        cp = store.load()
    except (CheckpointCorruptedError, FileNotFoundError, ValueError) as exc:
        raise ResumeRefused(f"checkpoint unusable: {exc}") from exc
    if cp.status in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED):
        raise ResumeRefused(f"run already terminal ({cp.status.value})")
    audit = AuditLog(run_dir / "audit" / "journal.jsonl")
    alignment = align_recovery(cp, audit)
    ctx = RunContext(
        run_id=cp.run_id,
        task=task,
        gate=gate,
        sandbox=sandbox,
        audit=audit,
        approvals=ApprovalStore(audit),
        max_turns=cp.budget["max_turns"],
        max_writes=cp.budget["max_writes"],
        max_tokens=cp.budget["max_tokens"],
    )
    loop = HarnessLoop(ctx, model, store)
    loop._stop_reason = cp.stop_reason
    if alignment.block_reason:
        # 悬挂 INTENT / 存档落后于结果 / 日志损坏：保守停止，不自动重放或重建
        apply_to_context(cp, ctx)
        ctx.state = RunState.PAUSED
        loop._needs_review(alignment.block_reason)
        return loop
    if workspace_changed(cp, sandbox, task):
        # C4：工作区已变化 → 旧证据与旧审批失效（停止等待核对；无绕过入口）
        apply_to_context(cp, ctx)
        ctx.state = RunState.PAUSED
        loop._needs_review("workspace changed since checkpoint; evidence and approvals invalidated")
        return loop
    apply_to_context(cp, ctx)
    loop.agent.memory = load_memory(cp.memory_payload or [])
    loop.plan = cp.plan
    # 应用日志中已持久化的人工决定（跨进程 approve/reject 后 resume 的路径）
    for proposal_id, verdict in alignment.decided_proposal_ids.items():
        proposal = ctx.approvals.proposals.get(proposal_id)
        if proposal is not None and proposal.status == ProposalStatus.PENDING:
            new_status = ProposalStatus.APPROVED if verdict == "approved" else ProposalStatus.REJECTED
            ctx.approvals.proposals[proposal_id] = replace(
                proposal, status=new_status, decided_by="user", decided_at=time()
            )
            if new_status == ProposalStatus.REJECTED:
                # 跨进程拒绝：补 observation 后解除挂起（与进程内 reject 同语义）
                loop._append_observation_step(
                    f"REJECTED proposal {proposal_id} for '{proposal.arguments.get('path')}': "
                    "the human rejected this write. Change approach; do not resubmit identical content."
                )
    if ctx.state == RunState.WAITING_APPROVAL and not any(
        p.status in (ProposalStatus.PENDING, ProposalStatus.APPROVED)
        for p in ctx.approvals.proposals.values()
    ):
        ctx.state = RunState.RUNNING  # 决定已齐（拒绝路径），可续跑
    # memory 还原：重建实例 ≠ 恢复成功，必须核对观察可见性（M0a E6）
    # 保留存档停止原因，resume 据此裁决是否允许继续。
    loop._started = True  # 恢复续跑必须 reset=False，否则记忆被清
    return loop

"""受控工具与运行上下文（PLAN §4/§6.1）。

每个工具 = 审计 → Gate → Sandbox → handler；拒绝返回结构化错误文本作为
observation（模型自纠），不抛异常。写操作不直接执行 handler：只登记提案、
进入 WAITING_APPROVAL，实际写入由工程侧在审批消费后代执行（loop.py）。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from time import time

from smolagents import Tool

from .audit import AuditLog
from .approval import ApprovalStore
from .gate import Gate
from .sandbox import CommandResult, Sandbox, SandboxRejection
from .types import Actor, AuditRecord, ConsumedCall, Decision, Evidence, RunState

# SerialAgent（loop.py）在 execute_tool_call 前设置；工具层读取以关联审计与提案
current_tool_call_id: ContextVar[str] = ContextVar("current_tool_call_id", default="")

MAX_READ_CHARS = 32000
MAX_OUTPUT_TAIL = 2000

# 受控工具参数 schema（Gate 输入）
TOOL_SCHEMAS: dict[str, dict[str, type]] = {
    "read_file": {"path": str},
    "write_file": {"path": str, "content": str},
    "run_test": {"scope": str},
}


@dataclass
class TaskSpec:
    """任务定义：模式、边界与测试命令（由 run 入口从任务文件解析）。"""

    mode: str  # "fix"（测试修复，读写）| "understand"（代码理解，只读源码）
    input_text: str
    workspace_root: Path
    artifact_target: str = "artifacts/result.md"  # understand 模式的产物路径（工作区相对）
    # understand 模式写入范围（None = fix 模式全工作区可写，受 protected 限制）
    write_allow_patterns: list[str] | None = None
    test_commands: dict[str, str] = field(default_factory=dict)  # scope → command_id
    evidence_patterns: list[str] = field(default_factory=lambda: ["*"])
    evidence_excludes: list[str] = field(default_factory=lambda: [
        "node_modules/**", "**/node_modules/**", ".venv/**", ".git/**", "state/**", "audit/**", "artifacts/**", "__pycache__/**", ".pytest_cache/**", "**/__pycache__/**", "**/.pytest_cache/**", "**/*.pyc",
    ])
    digest_patterns: list[str] = field(default_factory=lambda: ["*"])
    digest_excludes: list[str] = field(default_factory=lambda: [
        "node_modules/**", ".venv/**", "state/**", "audit/**", "artifacts/**",
        "__pycache__/**", ".pytest_cache/**", "**/__pycache__/**", "**/.pytest_cache/**", "**/*.pyc",
    ])


class RunContext:
    """工具层与循环层共享的运行时状态（恢复时由 state.py 从 checkpoint 还原）。"""

    def __init__(self, run_id: str, task: TaskSpec, gate: Gate, sandbox: Sandbox, audit: AuditLog,
                 approvals: ApprovalStore, max_turns: int, max_writes: int, max_tokens: int):
        self.run_id = run_id
        self.task = task
        self.gate = gate
        self.sandbox = sandbox
        self.audit = audit
        self.approvals = approvals
        self.state = RunState.RUNNING
        self.step = 0  # 已完成步数（Harness 轮数）
        self.evidence: list[Evidence] = []
        self.consumed_calls: list[ConsumedCall] = []  # 已消费调用（跨进程拒绝重复执行，G4）
        self.budget = {
            "turns_used": 0, "writes_used": 0, "tokens_used": 0,
            "max_turns": max_turns, "max_writes": max_writes, "max_tokens": max_tokens,
        }
        self.cancel_event = threading.Event()
        self.write_budget_refused = False  # 本步内出现过写预算拒绝（loop 据此停 BUDGET_EXHAUSTED）

    @property
    def consumed_call_ids(self) -> set[str]:
        return {c.tool_call_id for c in self.consumed_calls}

    # ------------------------------------------------ 审计便捷入口（贯穿 run/step/call）

    def record(self, rule: str, decision: Decision, actor: Actor, detail: str = "",
               tool_call_id: str | None = None) -> None:
        self.audit.append(
            AuditRecord(
                run_id=self.run_id,
                step=self.step,
                tool_call_id=tool_call_id if tool_call_id is not None else current_tool_call_id.get(),
                rule=rule,
                decision=decision,
                actor=actor,
                detail=detail,
                timestamp=time(),
            )
        )

    def writes_exhausted(self) -> bool:
        return self.budget["writes_used"] >= self.budget["max_writes"]

    def test_command_for(self, scope: str) -> str | None:
        return self.task.test_commands.get(scope)


def _rejection_observation(ctx: RunContext, rule: str, message: str) -> str:
    """拒绝的统一出口：留审计痕 + 返回明确错误 observation（handler 不执行）。"""
    ctx.record(rule, Decision.REJECT, Actor.HARNESS, message)
    return message


def validate_write_target(ctx: RunContext, path: str):
    """写目标统一校验（提案登记与审批后代执行复用同一规则，调用链一致）。

    返回 (真实落点, SandboxRejection | None)：容器/真实落点/敏感/受保护/写入范围。
    """
    real, srej = ctx.sandbox.check_path(path, for_write=True)
    if srej is not None:
        return None, srej
    allow = ctx.task.write_allow_patterns
    if allow is not None:
        rel = real.relative_to(ctx.sandbox.workspace_root).as_posix()
        if not any(fnmatch(rel, pat) or fnmatch(real.name, pat) for pat in allow):
            return None, SandboxRejection(
                rule="sandbox.write_out_of_scope",
                message=f"Rejected: in understand mode writes are limited to {allow}; source code is read-only.",
            )
    if ctx.task.mode == "understand":
        target = Path(os.path.normpath(ctx.sandbox.resolve(ctx.task.artifact_target)))
        requested = Path(os.path.normpath(ctx.sandbox.resolve(path)))
        if not ctx.task.artifact_target or requested != target or real != target:
            return None, SandboxRejection(
                rule="sandbox.write_out_of_scope",
                message=f"Rejected: in understand mode only '{ctx.task.artifact_target}' may be written.",
            )
    return real, None


class ControlledReadFile(Tool):
    name = "read_file"
    description = "读取工作区内一个文本文件的内容（只读）。"
    inputs = {"path": {"type": "string", "description": "工作区相对路径，如 src/calc.py"}}
    output_type = "string"

    def __init__(self, ctx: RunContext):
        super().__init__()
        self.ctx = ctx

    def forward(self, path: str) -> str:
        rejection = self.ctx.gate.check(self.name, {"path": path})
        if rejection:
            return _rejection_observation(self.ctx, rejection.rule, rejection.message)
        real, srej = self.ctx.sandbox.check_path(path, for_write=False)
        if srej:
            return _rejection_observation(self.ctx, srej.rule, srej.message)
        self.ctx.record(f"tool.{self.name}", Decision.ALLOW, Actor.HARNESS, f"read {real.relative_to(self.ctx.sandbox.workspace_root)}")
        try:
            content = real.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return _rejection_observation(self.ctx, f"tool.{self.name}", f"Error reading file: {exc}")
        if len(content) > MAX_READ_CHARS:
            content = content[:MAX_READ_CHARS] + f"\n...[truncated, {len(content) - MAX_READ_CHARS} chars omitted]"
        self.ctx.evidence.append(
            Evidence(kind="source_read", scope=str(real), tool_call_id=current_tool_call_id.get(), timestamp=time())
        )
        self.ctx.record(f"tool.{self.name}", Decision.EXECUTED, Actor.HARNESS, f"{len(content)} chars")
        return content


class ControlledWriteFile(Tool):
    name = "write_file"
    description = (
        "向工作区写入文件（整文件覆盖）。写操作需要人工审批：调用后进入 WAITING_APPROVAL，"
        "审批通过后由系统执行；同一路径的现场必须与提案时一致。"
    )
    inputs = {
        "path": {"type": "string", "description": "工作区相对路径"},
        "content": {"type": "string", "description": "完整文件内容"},
    }
    output_type = "string"

    def __init__(self, ctx: RunContext):
        super().__init__()
        self.ctx = ctx

    def forward(self, path: str, content: str) -> str:
        ctx = self.ctx
        call_id = current_tool_call_id.get()
        rejection = ctx.gate.check(self.name, {"path": path, "content": content})
        if rejection:
            return _rejection_observation(ctx, rejection.rule, rejection.message)
        if call_id and call_id in ctx.consumed_call_ids:
            return _rejection_observation(ctx, "call.duplicate", f"Rejected: call '{call_id}' was already executed; duplicate submission is not allowed.")
        if ctx.writes_exhausted():
            ctx.write_budget_refused = True
            return _rejection_observation(ctx, "budget.writes_exhausted",
                                          "Rejected: write budget exhausted. Finish with what you have or summarize findings.")
        real, srej = validate_write_target(ctx, path)
        if srej:
            return _rejection_observation(ctx, srej.rule, srej.message)
        existing = real.read_bytes() if real.exists() else b""
        proposal = ctx.approvals.register(
            run_id=ctx.run_id,
            step=ctx.step,
            tool_call_id=call_id,
            tool_name=self.name,
            arguments={"path": path, "content": content},
            target_realpath=str(real),
            file_state_digest=_digest_bytes(existing),
        )
        ctx.state = RunState.WAITING_APPROVAL
        return (
            f"WAIT_APPROVAL proposal_id={proposal.proposal_id}: write to '{path}' "
            f"({len(content)} chars) is pending human approval. The run is paused; "
            "wait for the decision, do not call other tools."
        )


class ControlledRunTest(Tool):
    name = "run_test"
    description = "运行项目测试（配置内固定命令，按 scope 选择）。返回退出码与输出摘要。"
    inputs = {"scope": {"type": "string", "description": "测试范围标识，如 'default'（见任务配置）"}}
    output_type = "string"

    def __init__(self, ctx: RunContext):
        super().__init__()
        self.ctx = ctx

    def forward(self, scope: str) -> str:
        ctx = self.ctx
        rejection = ctx.gate.check(self.name, {"scope": scope})
        if rejection:
            return _rejection_observation(ctx, rejection.rule, rejection.message)
        command_id = ctx.test_command_for(scope)
        if command_id is None:
            return _rejection_observation(ctx, "sandbox.cmd_not_allowed",
                                          f"Rejected: test scope '{scope}' is not configured. Available: {sorted(ctx.task.test_commands)}.")
        ctx.record(f"tool.{self.name}", Decision.ALLOW, Actor.HARNESS, f"scope={scope} command={command_id}")
        digest_before = ctx.sandbox.digest_fileset(ctx.task.evidence_patterns, ctx.task.evidence_excludes)
        guarded_before = ctx.sandbox.guarded_path_snapshot()
        result = ctx.sandbox.run_command(command_id, cancel_event=ctx.cancel_event)
        if isinstance(result, SandboxRejection):
            return _rejection_observation(ctx, result.rule, result.message)
        assert isinstance(result, CommandResult)
        digest_after = ctx.sandbox.digest_fileset(ctx.task.evidence_patterns, ctx.task.evidence_excludes)
        content_unchanged = digest_after == digest_before
        guarded_after = ctx.sandbox.guarded_path_snapshot()
        changes = {
            "added": sorted(guarded_after.keys() - guarded_before.keys()),
            "modified": sorted(p for p in guarded_before.keys() & guarded_after.keys()
                               if guarded_before[p] != guarded_after[p]),
            "deleted": sorted(guarded_before.keys() - guarded_after.keys()),
        }
        guarded_changed = any(changes.values())
        passed = (result.exit_code == 0 and not result.timed_out and not result.cancelled
                  and content_unchanged and not guarded_changed)
        summary = _tail(result.stdout + ("\n" + result.stderr if result.stderr else ""))
        if result.timed_out:
            summary = f"TIMEOUT after {result.duration_s:.1f}s\n" + summary
        if result.cancelled:
            summary = "CANCELLED by user\n" + summary
        if not content_unchanged:
            summary = "WARNING: workspace content changed during test run; result not recorded as pass\n" + summary
        if guarded_changed:
            ctx.record("sandbox.test_guarded_paths_changed", Decision.REJECT, Actor.HARNESS, json.dumps(changes))
            summary = "WARNING: sensitive or protected paths changed; result not recorded as pass\n" + summary
        ctx.evidence.append(
            Evidence(
                kind="test_run",
                scope=scope,
                command_id=command_id,
                exit_code=result.exit_code,
                passed=passed,
                content_digest=digest_after,
                output_summary=summary,
                tool_call_id=current_tool_call_id.get(),
                timestamp=time(),
            )
        )
        ctx.record(f"tool.{self.name}", Decision.EXECUTED, Actor.HARNESS,
                   f"exit={result.exit_code} passed={passed} timed_out={result.timed_out} cancelled={result.cancelled}")
        verdict = "PASS" if passed else "FAIL"
        return f"run_test[{scope}] {verdict} (exit_code={result.exit_code})\n{summary}"


def make_tools(ctx: RunContext) -> list[Tool]:
    return [ControlledReadFile(ctx), ControlledWriteFile(ctx), ControlledRunTest(ctx)]


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tail(text: str, limit: int = MAX_OUTPUT_TAIL) -> str:
    return text[-limit:] if len(text) > limit else text

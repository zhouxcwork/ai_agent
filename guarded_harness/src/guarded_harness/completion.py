"""完成判定（H1/H2，PLAN §6.4）。

挂 smolagents 原生 final_answer_checks：检查失败 raise → 框架包装 AgentError →
循环捕获后继续跑（与 H1"拒绝后不能进入完成态"吻合，M0a E4 已验证时序）。
规则：每个要求的测试范围取全历史最新一次证据（先取最新再判 passed），
必须通过且 content_digest 与当前受测内容一致（H2：写入后旧证据失效）。
"""

from __future__ import annotations

from .tools import RunContext


def latest_test_evidence(ctx: RunContext, scope: str):
    """该 scope 全历史最新一次 test_run 证据（不先筛通过记录）。"""
    runs = [e for e in ctx.evidence if e.kind == "test_run" and e.scope == scope]
    return runs[-1] if runs else None


def check_completion(ctx: RunContext) -> None:
    """程序侧最终验收。不通过 raise ValueError（框架转为 AgentError 并继续循环）。"""
    problems: list[str] = []
    if not ctx.task.artifact_target:
        problems.append("no artifact_target configured")
    else:
        real, rejection = ctx.sandbox.check_path(ctx.task.artifact_target, for_write=False)
        if rejection is not None or not real.is_file() or not real.read_text(encoding="utf-8").strip():
            problems.append(f"deliverable '{ctx.task.artifact_target}' missing or empty")
    if ctx.state.value != "RUNNING":
        problems.append("run is not RUNNING")
    if ctx.task.mode == "fix":
        if not ctx.task.test_commands:
            problems.append("no test commands configured")
        current_digest = ctx.sandbox.digest_fileset(ctx.task.evidence_patterns, ctx.task.evidence_excludes)
        for scope in ctx.task.test_commands:
            latest = latest_test_evidence(ctx, scope)
            if latest is None:
                problems.append(f"no test evidence for scope '{scope}': run_test before final answer")
            elif not latest.passed:
                problems.append(f"latest test run for '{scope}' failed (exit={latest.exit_code}); fix and re-run")
            elif latest.content_digest != current_digest:
                problems.append(
                    f"test evidence for '{scope}' is stale: workspace content changed after the last run; re-run tests"
                )
    elif ctx.task.mode == "understand":
        if not ctx.task.artifact_target:
            problems.append("understand task has no artifact_target configured")
        else:
            real, rejection = ctx.sandbox.check_path(ctx.task.artifact_target, for_write=False)
            if rejection is not None:
                problems.append(f"artifact path invalid: {rejection.message}")
            elif not real.exists() or real.stat().st_size == 0:
                problems.append(
                    f"deliverable '{ctx.task.artifact_target}' missing or empty; write it (subject to approval) first"
                )
        if not any(e.kind == "source_read" for e in ctx.evidence):
            problems.append("no source reading evidence; read the relevant sources before final answer")
    else:
        problems.append(f"unknown task mode '{ctx.task.mode}'")
    if problems:
        raise ValueError("; ".join(problems))


def make_final_answer_check(ctx: RunContext):
    """final_answer_checks 挂钩：签名符合 smolagents 约定。"""

    def _check(final_answer, memory, agent=None):
        check_completion(ctx)
        return True

    _check.__name__ = "guarded_completion_check"
    return _check

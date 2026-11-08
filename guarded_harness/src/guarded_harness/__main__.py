"""薄命令行入口（PLAN §10.1）：run / approve / reject / resume。

只做参数解析与装配，业务逻辑在 loop.py / approval.py。
配置（任务、命令白名单、预算）随 run 落盘 run_dir/run_config.json，resume 复用。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .approval import ApprovalError, ApprovalStore
from .audit import AuditLog
from .gate import Gate
from .loop import HarnessLoop, ResumeRefused, build_context, load_run
from .sandbox import CommandSpec, Sandbox
from .state import CheckpointStore, align_recovery
from .tools import TOOL_SCHEMAS, TaskSpec
from .types import ProposalStatus, RunState

CONFIG_FILE = "run_config.json"

# 退出码：0=完成；3=等待人工（审批/暂停）；1=失败/取消；2=用法错误由 argparse
EXIT_DONE = 0
EXIT_HUMAN = 3
EXIT_BAD = 1


def _parse_test_command(spec: str, timeout: float) -> tuple[str, CommandSpec]:
    """'scope=argv0|argv1' → (scope, CommandSpec)。"""
    scope, _, argv_text = spec.partition("=")
    argv = [part for part in argv_text.split("|") if part]
    if not scope or not argv:
        raise SystemExit(f"--test-command 格式应为 'scope=argv0|argv1'，收到: {spec!r}")
    return scope, CommandSpec(command_id=scope, argv=argv, timeout_s=timeout)


def _load_config(run_dir: Path) -> dict:
    path = run_dir / CONFIG_FILE
    if not path.exists():
        raise SystemExit(f"缺少运行配置 {path}；resume/approve 需要用 run 创建的运行目录")
    return json.loads(path.read_text(encoding="utf-8"))


def _build_task_and_sandbox(cfg: dict, workspace: Path) -> tuple[TaskSpec, Sandbox]:
    commands = {
        c["scope"]: CommandSpec(command_id=c["scope"], argv=c["argv"], timeout_s=c["timeout_s"])
        for c in cfg["test_commands"]
    }
    mode = cfg["mode"]
    task = TaskSpec(
        mode=mode,
        input_text=cfg["goal"],
        workspace_root=workspace,
        artifact_target=cfg.get("artifact_target") or "artifacts/result.md",
        write_allow_patterns=["artifacts/**"] if mode == "understand" else None,
        test_commands={scope: scope for scope in commands},
        evidence_patterns=cfg.get("evidence_patterns") or ["src/**", "tests/**", "*.json", "*.toml", "*.lock"],
        evidence_excludes=cfg.get("evidence_excludes") or ["node_modules/**", ".venv/**", "state/**", "**/__pycache__/**", "**/.pytest_cache/**", "**/*.pyc"],
        digest_patterns=cfg.get("digest_patterns") or ["src/**", "tests/**", "*.json", "*.toml", "*.lock"],
        digest_excludes=cfg.get("digest_excludes") or ["node_modules/**", ".venv/**", "state/**", "audit/**", "artifacts/**", "**/__pycache__/**", "**/.pytest_cache/**", "**/*.pyc"],
    )
    sandbox = Sandbox(
        workspace_root=workspace,
        commands=commands,
        sensitive_paths=cfg.get("sensitive_paths", [".env"]),
        protected_patterns=cfg.get("protected_patterns", []),
    )
    return task, sandbox


def _build_model(cfg: dict):
    from smolagents import OpenAIServerModel

    base_url = os.environ.get(cfg.get("base_url_env", "OPENAI_BASE_URL"), "https://api.deepseek.com")
    api_key_env = cfg.get("api_key_env", "DEEPSEEK_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise SystemExit(f"环境变量 {api_key_env} 未设置（密钥只从环境注入，不落盘不打印）")
    return OpenAIServerModel(model_id=cfg.get("model_id", "deepseek-chat"), api_key=api_key, api_base=base_url)


def _exit_code_for(state: RunState) -> int:
    if state == RunState.COMPLETED:
        return EXIT_DONE
    if state in (RunState.WAITING_APPROVAL, RunState.PAUSED):
        return EXIT_HUMAN
    return EXIT_BAD


def cmd_run(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise SystemExit(f"运行目录已存在且非空，拒绝覆盖: {run_dir}")
    source = Path(args.source).resolve()
    if not source.is_dir():
        raise SystemExit(f"源仓库不存在: {source}")
    goal = args.goal
    if args.goal_file:
        goal = json.loads(Path(args.goal_file).read_text(encoding="utf-8"))["goal"]
    if not goal:
        raise SystemExit("需要 --goal 或 --goal-file 提供任务输入")

    if args.mode == "fix" and not args.test_command:
        raise SystemExit("fix 模式必须提供 --test-command")
    workspace = run_dir / "workspace"
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, workspace)

    commands = {}
    for spec in args.test_command or []:
        scope, cmd = _parse_test_command(spec, args.timeout)
        commands[scope] = cmd
    cfg = {
        "mode": args.mode,
        "goal": goal,
        "artifact_target": args.artifact_target or "artifacts/result.md",
        "test_commands": [
            {"scope": scope, "argv": cmd.argv, "timeout_s": cmd.timeout_s} for scope, cmd in commands.items()
        ],
        "sensitive_paths": args.sensitive or [".env"],
        "protected_patterns": args.protected or [],
        "max_turns": args.max_turns,
        "max_writes": args.max_writes,
        "max_tokens": args.max_tokens,
        "model_id": args.model_id,
        "api_key_env": args.api_key_env,
        "base_url_env": args.base_url_env,
    }
    (run_dir / CONFIG_FILE).write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")

    task, sandbox = _build_task_and_sandbox(cfg, workspace)
    ctx = build_context(
        run_id=run_dir.name, task=task, run_dir=run_dir, gate=Gate(TOOL_SCHEMAS), sandbox=sandbox,
        max_turns=args.max_turns, max_writes=args.max_writes, max_tokens=args.max_tokens,
    )
    from .types import Actor, Decision

    ctx.record("run.started", Decision.ALLOW, Actor.HARNESS, f"mode={args.mode} source={source}")
    loop = HarnessLoop(ctx, _build_model(cfg), CheckpointStore(run_dir / "state"))
    outcome = loop.run()
    print(outcome.summary())
    return _exit_code_for(outcome.state)


def _decide(args: argparse.Namespace, approved: bool) -> int:
    run_dir = Path(args.run_dir).resolve()
    cfg = _load_config(run_dir)
    cp = CheckpointStore(run_dir / "state").load()
    audit = AuditLog(run_dir / "audit" / "journal.jsonl")
    alignment = align_recovery(cp, audit)
    if cp.status != RunState.WAITING_APPROVAL or alignment.block_reason:
        print("决定被拒绝: 运行未处于可审批状态或日志需要核对", file=sys.stderr)
        return EXIT_BAD
    if args.proposal_id in alignment.decided_proposal_ids:
        print("决定被拒绝: 此提案已有持久化决定", file=sys.stderr)
        return EXIT_BAD
    store = ApprovalStore(audit, proposals={p.proposal_id: p for p in cp.pending_approvals})
    try:
        proposal = store.decide(cp.run_id, args.proposal_id, approved=approved, by=args.by, step=cp.step)
    except ApprovalError as exc:
        print(f"决定被拒绝: {exc}", file=sys.stderr)
        return EXIT_BAD
    print(f"已记录人工决定: {proposal.proposal_id} -> {'approved' if approved else 'rejected'}")
    print("继续执行请运行: python -m guarded_harness resume", run_dir)
    return EXIT_DONE


def cmd_resume(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    cfg = _load_config(run_dir)
    workspace = run_dir / "workspace"
    task, sandbox = _build_task_and_sandbox(cfg, workspace)
    try:
        loop = load_run(run_dir, _build_model(cfg), gate=Gate(TOOL_SCHEMAS), sandbox=sandbox, task=task)
        outcome = loop.resume()
    except ResumeRefused as exc:
        print(f"恢复被拒绝: {exc}", file=sys.stderr)
        return EXIT_BAD
    print(outcome.summary())
    return _exit_code_for(outcome.state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="guarded_harness", description="Guarded Coding Harness（作业三）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="新任务：复制源仓库到运行目录并驱动")
    p_run.add_argument("--mode", choices=["fix", "understand"], required=True)
    p_run.add_argument("--goal", default="", help="任务输入文本")
    p_run.add_argument("--goal-file", help="任务 JSON（读 goal 字段），如 samples/2_bug_fix/task_fix.json")
    p_run.add_argument("--source", required=True, help="被测源仓库目录")
    p_run.add_argument("--run-dir", required=True, help="运行目录（workspace/state/audit 之父）")
    p_run.add_argument("--test-command", action="append",
                       help="测试命令白名单 'scope=argv0|argv1'，可多次；understand 可省略")
    p_run.add_argument("--timeout", type=float, default=180.0, help="测试命令超时秒数")
    p_run.add_argument("--artifact-target", default="", help="交付物路径（工作区相对，默认 artifacts/result.md）")
    p_run.add_argument("--protected", action="append", default=None, help="受保护文件 glob，可多次")
    p_run.add_argument("--sensitive", action="append", default=None, help="敏感路径，可多次")
    p_run.add_argument("--max-turns", type=int, default=12)
    p_run.add_argument("--max-writes", type=int, default=6)
    p_run.add_argument("--max-tokens", type=int, default=200000)
    p_run.add_argument("--model-id", default="deepseek-chat")
    p_run.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    p_run.add_argument("--base-url-env", default="OPENAI_BASE_URL")
    p_run.set_defaults(func=cmd_run)

    for name, approved in (("approve", True), ("reject", False)):
        p = sub.add_parser(name, help=f"记录人工{'批准' if approved else '拒绝'}决定（跨进程持久化）")
        p.add_argument("run_dir")
        p.add_argument("proposal_id")
        p.add_argument("--by", default="user")
        p.set_defaults(func=lambda args, approved=approved: _decide(args, approved))

    p_resume = sub.add_parser("resume", help="恢复运行（执行已批准提案并续跑）")
    p_resume.add_argument("run_dir")
    p_resume.set_defaults(func=cmd_resume)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

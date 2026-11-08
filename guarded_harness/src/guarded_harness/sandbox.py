"""沙箱（S1-S4）：路径规范化与真实落点校验、敏感路径、命令白名单、进程组管理。

边界声明（PLAN §7.1）：路径门禁与进程组管理不等于系统级任意代码隔离；
run_test 执行的是受信 samples 的固定命令数组。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SandboxRejection:
    rule: str  # sandbox.path_escape / sandbox.symlink_escape / sandbox.sensitive_path / sandbox.protected_path / sandbox.cmd_not_allowed
    message: str
    detail: dict | None = None


@dataclass
class CommandSpec:
    command_id: str
    argv: list[str]
    timeout_s: float
    cwd: str = ""  # 相对 workspace_root；空 = workspace_root


@dataclass
class CommandResult:
    command_id: str
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    duration_s: float = 0.0


def _under(path: Path, root: Path) -> bool:
    """path 是否等于 root 或位于 root 之下（规范化后的容器检查）。"""
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:  # 不同盘符/混合绝对相对，视为界外
        return False


class Sandbox:
    def __init__(
        self,
        workspace_root: Path,
        commands: dict[str, CommandSpec],
        sensitive_paths: list[str] | None = None,
        protected_patterns: list[str] | None = None,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        self.commands = commands
        # 敏感路径：绝对路径原样；相对路径锚定 workspace_root（真实落点比对）
        self.sensitive_paths: list[Path] = []
        for raw in sensitive_paths or []:
            p = Path(raw).expanduser()
            self.sensitive_paths.append(p if p.is_absolute() else self.workspace_root / p)
        self.sensitive_paths = [Path(os.path.realpath(p)) for p in self.sensitive_paths]
        # 受保护文件（写拒绝）：相对 workspace 的 glob 模式，如 "tests/*.test.ts"、"package.json"
        self.protected_patterns = protected_patterns or []

    # ------------------------------------------------------------ 路径门禁（S1/S2）

    def resolve(self, rel_path: str) -> Path:
        """规范化路径：绝对路径原样，相对路径锚定 workspace_root。"""
        p = Path(rel_path)
        return p if p.is_absolute() else self.workspace_root / p

    def check_path(self, rel_path: str, *, for_write: bool) -> tuple[Path | None, SandboxRejection | None]:
        """返回 (真实落点, 拒绝)。规范化容器检查 → realpath 落点检查 → 敏感路径 → 受保护文件。"""
        root = self.workspace_root
        candidate = Path(os.path.normpath(str(self.resolve(rel_path))))
        if not _under(candidate, root):
            return None, SandboxRejection(
                rule="sandbox.path_escape",
                message=f"Rejected: path '{rel_path}' resolves outside the workspace.",
                detail={"normalized": str(candidate), "workspace_root": str(root)},
            )
        real = Path(os.path.realpath(str(candidate)))  # 真实落点：解析已存在的各级软链接
        if not _under(real, root):
            return None, SandboxRejection(
                rule="sandbox.symlink_escape",
                message=f"Rejected: real target of '{rel_path}' escapes the workspace.",
                detail={"realpath": str(real), "workspace_root": str(root)},
            )
        for sensitive in self.sensitive_paths:
            if real == sensitive or sensitive in real.parents:
                return None, SandboxRejection(
                    rule="sandbox.sensitive_path",
                    message=f"Rejected: path '{rel_path}' targets a sensitive location.",
                    detail={"realpath": str(real)},
                )
        if for_write:
            posix_rel = real.relative_to(root).as_posix()
            for pattern in self.protected_patterns:
                if fnmatch.fnmatch(posix_rel, pattern) or fnmatch.fnmatch(real.name, pattern):
                    return None, SandboxRejection(
                        rule="sandbox.protected_path",
                        message=(
                            f"Rejected: '{posix_rel}' is a protected file (original tests / test entry / lockfile). "
                            "Add new files instead of modifying protected ones."
                        ),
                        detail={"pattern": pattern},
                    )
        return real, None

    # ------------------------------------------------------------ 命令白名单与进程组（S3/S4）

    def check_command(self, command_id: str) -> CommandSpec | SandboxRejection:
        spec = self.commands.get(command_id)
        if spec is None:
            return SandboxRejection(
                rule="sandbox.cmd_not_allowed",
                message=(
                    f"Rejected: command '{command_id}' is not in the allowed set {sorted(self.commands)}. "
                    "Use one of the configured command ids."
                ),
                detail={"command_id": command_id},
            )
        return spec

    def run_command(self, command_id: str, cancel_event=None) -> CommandResult | SandboxRejection:
        """执行白名单命令。超时与取消都 killpg 整个进程组并回收，不留幸存者（S4）。"""
        checked = self.check_command(command_id)
        if isinstance(checked, SandboxRejection):
            return checked
        spec = checked
        cwd = self.workspace_root / spec.cwd if spec.cwd else self.workspace_root
        start = time.monotonic()
        proc = subprocess.Popen(  # noqa: S603 - argv 来自配置固定数组，非模型输入
            spec.argv,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # 独立进程组，组内全体可回收
        )
        timed_out = cancelled = False
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if cancel_event is not None and cancel_event.is_set():
                    self._kill_group(proc)
                    cancelled = True
                elif time.monotonic() - start >= spec.timeout_s:
                    self._kill_group(proc)
                    timed_out = True
                if cancelled or timed_out:
                    stdout, stderr = proc.communicate()  # 回收尸与残余输出
                    break
        return CommandResult(
            command_id=command_id,
            exit_code=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=timed_out,
            cancelled=cancelled,
            duration_s=time.monotonic() - start,
        )

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # start_new_session 下 pgid == pid
        except (ProcessLookupError, PermissionError):
            pass

    # ------------------------------------------------------------ 指纹（C4 / H2 载体）

    def digest_fileset(self, patterns: list[str], exclude_patterns: list[str] | None = None) -> str:
        """约定文件集合的联合指纹：相对路径 + 内容 sha256，排序后整体散列。

        排除 checkpoint/审计/缓存/node_modules/venv 由调用方以 exclude_patterns 表达。
        """
        exclude_patterns = exclude_patterns or []
        entries = []
        for path in sorted(self.workspace_root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(self.workspace_root).as_posix()
            if any(fnmatch.fnmatch(rel, pat) for pat in exclude_patterns):
                continue
            if not any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(path.name, pat) for pat in patterns):
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append(f"{rel}:{digest}")
        return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()

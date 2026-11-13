"""S1-S4 沙箱测试：路径逃逸、软链接/敏感路径、命令白名单、进程组终止。"""

import os
import sys
import threading
import time

from guarded_harness.approval import ApprovalStore
from guarded_harness.audit import AuditLog
from guarded_harness.gate import Gate
from guarded_harness.sandbox import CommandResult, CommandSpec, Sandbox, SandboxRejection


def make_sandbox(mini) -> Sandbox:
    return Sandbox(
        workspace_root=mini.workspace,
        commands=mini.commands,
        sensitive_paths=[".env"],
        protected_patterns=mini.protected,
    )


class TestS1PathEscape:
    def test_artifacts_dotdot_escape_rejected(self, mini):
        box = make_sandbox(mini)
        real, rejection = box.check_path("artifacts/../../.env", for_write=False)
        assert real is None
        assert rejection.rule == "sandbox.path_escape"
        assert "artifacts/../../.env" in rejection.message

    def test_escape_target_untouched(self, mini):
        box = make_sandbox(mini)
        before = mini.secret_env.read_bytes()
        box.check_path("artifacts/../../.env", for_write=True)
        assert mini.secret_env.read_bytes() == before  # 拒绝后文件不变

    def test_absolute_path_outside_rejected(self, mini):
        box = make_sandbox(mini)
        _, rejection = box.check_path(str(mini.secret_env), for_write=False)
        assert rejection.rule == "sandbox.path_escape"

    def test_in_workspace_path_allowed(self, mini):
        box = make_sandbox(mini)
        real, rejection = box.check_path("src/calc.py", for_write=False)
        assert rejection is None and real == mini.workspace / "src" / "calc.py"


class TestS2SymlinkAndSensitive:
    def test_symlink_escape_rejected_by_realpath(self, mini, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = mini.workspace / "link_to_outside.txt"
        link.symlink_to(outside)
        box = make_sandbox(mini)
        real, rejection = box.check_path("link_to_outside.txt", for_write=False)
        assert real is None
        assert rejection.rule == "sandbox.symlink_escape"

    def test_sensitive_path_rejected(self, mini):
        (mini.workspace / ".env").write_text("FAKE=not-real\n", encoding="utf-8")
        box = make_sandbox(mini)
        real, rejection = box.check_path(".env", for_write=False)
        assert real is None
        assert rejection.rule == "sandbox.sensitive_path"

    def test_path_inside_symlink_dir_resolved(self, mini, tmp_path):
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "f.txt").write_text("x", encoding="utf-8")
        (mini.workspace / "symdir").symlink_to(outside_dir)
        box = make_sandbox(mini)
        _, rejection = box.check_path("symdir/f.txt", for_write=False)
        assert rejection.rule == "sandbox.symlink_escape"


class TestS3CommandWhitelist:
    def test_command_not_in_allowlist_rejected_without_execution(self, mini):
        box = make_sandbox(mini)
        canary = mini.workspace / "canary.txt"
        assert not canary.exists()
        # 白名单外命令：即使伪造 CommandSpec 也不该被执行——check_command 是唯一入口
        result = box.run_command("npm_test")
        assert isinstance(result, SandboxRejection)
        assert result.rule == "sandbox.cmd_not_allowed"
        assert not canary.exists()

    def test_allowed_command_executes(self, mini):
        box = make_sandbox(mini)
        result = box.run_command("run_pytest")
        assert isinstance(result, CommandResult)
        assert result.exit_code != 0  # mini 项目天然 1 败
        assert "1 failed" in result.stdout or "failed" in result.stdout
        assert not (result.timed_out or result.cancelled)

    def test_check_command_rejects_unknown_id(self, mini):
        box = make_sandbox(mini)
        assert isinstance(box.check_command("curl"), SandboxRejection)


class TestProtectedFiles:
    def test_write_to_original_test_rejected(self, mini):
        box = make_sandbox(mini)
        _, rejection = box.check_path("tests/test_calc_fail.py", for_write=True)
        assert rejection.rule == "sandbox.protected_path"

    def test_write_to_lockfile_and_entry_rejected(self, mini):
        box = make_sandbox(mini)
        assert box.check_path("requirements.lock", for_write=True)[1].rule == "sandbox.protected_path"
        assert box.check_path("pyproject.toml", for_write=True)[1].rule == "sandbox.protected_path"

    def test_new_test_file_allowed(self, mini):
        box = make_sandbox(mini)
        real, rejection = box.check_path("tests/test_new_check.py", for_write=True)
        assert rejection is None
        assert real == mini.workspace / "tests" / "test_new_check.py"

    def test_source_file_writable(self, mini):
        box = make_sandbox(mini)
        assert box.check_path("src/calc.py", for_write=True)[1] is None


class TestS4ProcessGroup:
    def _hang_spec(self, timeout_s: float) -> CommandSpec:
        # 自报 pid 并派生子进程再自报，父进程睡 30s：验证 killpg 不留幸存者
        script = (
            "import os,sys,time,subprocess\n"
            "child=subprocess.Popen([sys.executable,'-c',"
            "'import os,time;print(os.getpid(),flush=True);time.sleep(30)'],stdout=sys.stdout)\n"
            "print(os.getpid(),flush=True)\n"
            "time.sleep(30)\n"
        )
        return CommandSpec(command_id="hang", argv=[sys.executable, "-c", script], timeout_s=timeout_s)

    @staticmethod
    def _pid_dead(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return False
        except ProcessLookupError:
            return True

    def _run_and_verify_group_killed(self, box: Sandbox, expect_cancelled: bool):
        result = box.run_command("hang")
        assert isinstance(result, CommandResult)
        assert result.cancelled is expect_cancelled
        assert result.timed_out is not expect_cancelled
        pids = [int(x) for x in result.stdout.split() if x.isdigit()]
        assert len(pids) == 2, f"应捕获父/子两个 pid，stdout={result.stdout!r}"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not all(self._pid_dead(p) for p in pids):
            time.sleep(0.1)
        assert all(self._pid_dead(p) for p in pids), f"幸存进程: {pids}"

    def test_timeout_kills_whole_group(self, tmp_path):
        (tmp_path / "workspace").mkdir()
        box = Sandbox(workspace_root=tmp_path / "workspace", commands={"hang": self._hang_spec(1.0)})
        self._run_and_verify_group_killed(box, expect_cancelled=False)

    def test_cancel_kills_whole_group(self, tmp_path):
        (tmp_path / "workspace").mkdir()
        cancel = threading.Event()
        box = Sandbox(workspace_root=tmp_path / "workspace", commands={"hang": self._hang_spec(30.0)})

        def canceller():
            time.sleep(0.5)
            cancel.set()

        threading.Thread(target=canceller, daemon=True).start()
        result = box.run_command("hang", cancel_event=cancel)
        assert isinstance(result, CommandResult)
        assert result.cancelled and not result.timed_out
        pids = [int(x) for x in result.stdout.split() if x.isdigit()]
        assert len(pids) == 2, f"应捕获父/子两个 pid，stdout={result.stdout!r}"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not all(self._pid_dead(p) for p in pids):
            time.sleep(0.1)
        assert all(self._pid_dead(p) for p in pids), f"幸存进程: {pids}"


class TestFilesetDigest:
    def test_digest_changes_on_tracked_file_change(self, mini):
        box = make_sandbox(mini)
        before = box.digest_fileset(["src/**", "tests/**"], ["state/**"])
        (mini.workspace / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        after = box.digest_fileset(["src/**", "tests/**"], ["state/**"])
        assert before != after

    def test_digest_stable_when_only_untracked_changes(self, mini):
        box = make_sandbox(mini)
        before = box.digest_fileset(["src/**", "tests/**"], ["state/**"])
        state_dir = mini.workspace / "state"
        state_dir.mkdir()
        (state_dir / "checkpoint.json").write_text("{}", encoding="utf-8")
        after = box.digest_fileset(["src/**", "tests/**"], ["state/**"])
        assert before == after


class TestRunTestIntegrity:
    def test_guarded_paths_outside_evidence_are_audited(self, ctx):
        import json

        from conftest import call_id

        from guarded_harness.tools import ControlledRunTest

        root = ctx.sandbox.workspace_root
        (root / ".env").write_text("original secret")
        (root / "old.lock").write_text("old")
        ctx.sandbox = Sandbox(root, {"mutate": CommandSpec(
            "mutate", [sys.executable, "-c", "from pathlib import Path; "
                       "Path('.env').write_text('changed secret'); "
                       "Path('old.lock').unlink(); Path('new.lock').touch()"], 30,
        )}, sensitive_paths=[".env"], protected_patterns=["*.lock"])
        ctx.task.test_commands = {"default": "mutate"}
        ctx.task.evidence_patterns = ["src/**"]

        with call_id("guarded-test"):
            observation = ControlledRunTest(ctx)(scope="default", sanitize_inputs_outputs=True)
        assert "FAIL (exit_code=0)" in observation
        assert not ctx.evidence[-1].passed
        record, = [r for _, r in ctx.audit.read_all() if r.rule == "sandbox.test_guarded_paths_changed"]
        assert record.tool_call_id == "guarded-test"
        assert json.loads(record.detail) == {"added": ["new.lock"], "modified": [".env"], "deleted": ["old.lock"]}

    def test_content_change_during_test_not_recorded_as_pass(self, mini):
        """测试前后核对受测内容：执行中被改动的内容不登记有效通过。"""
        from conftest import call_id

        from guarded_harness.tools import ControlledRunTest, RunContext, TaskSpec

        mutating = CommandSpec(
            command_id="mutate",
            argv=[sys.executable, "-c",
                  "from pathlib import Path\nPath('src/calc.py').write_text('mutated during test')"],
            timeout_s=30.0,
        )
        commands = dict(mini.commands)
        commands["mutate"] = mutating
        ctx = RunContext(
            run_id="run-integrity",
            task=TaskSpec(mode="fix", input_text="t", workspace_root=mini.workspace,
                          test_commands={"default": "mutate"}),
            gate=Gate({"run_test": {"scope": str}}),
            sandbox=Sandbox(mini.workspace, commands),
            audit=AuditLog(mini.root / "run-integrity" / "audit" / "journal.jsonl"),
            approvals=ApprovalStore(AuditLog(mini.root / "run-integrity" / "audit" / "journal.jsonl")),
            max_turns=5, max_writes=5, max_tokens=10**9,
        )
        tool = ControlledRunTest(ctx)
        with call_id("it1"):
            observation = tool(scope="default", sanitize_inputs_outputs=True)
        assert "not recorded as pass" in observation
        assert ctx.evidence[-1].passed is False  # exit 0 也不算通过
        assert ctx.evidence[-1].content_digest is not None  # 证据仍绑当时内容指纹

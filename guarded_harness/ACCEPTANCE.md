# 作业三验收指南

本作业在不修改 smolagents 1.26.0 核心源码的前提下，为 `ToolCallingAgent` 增加可恢复的受控执行层。建议先运行确定性测试，再核对真实运行证据；无需先读实施计划。

[返回项目 README](README.md) · [作业要求原文](<../assignments/AI Agent 全栈工程师训练营-作业三.md>)

## 一分钟运行测试

环境：已验证 macOS arm64、Python 3.12.13；使用 uv 管理依赖。测试会创建临时被测项目和子进程，不请求真实模型，不需要密钥或 Node.js。首次安装可能需要网络。

```bash
cd guarded_harness  # 从 homework 仓库根目录执行
uv sync --locked --extra dev
uv run pytest -q
```

当前预期 **101 passed**，耗时因机器而异。2026-11-08 在不含原虚拟环境及参考源码的新临时项目实际执行 `uv run python -m pytest -q`，结果为 **101 passed in 3.37s**：[原始输出](records/run-final-verified/pytest_output.txt)。`pytest -q` 与 `python -m pytest -q` 使用同一套项目配置与测试集。

收集范围可另行检查：

```bash
uv run pytest --collect-only -q
```

应仅收集本作业六个测试文件的 101 个用例；不包含 smolagents 上游测试。子进程终止测试使用 POSIX 进程组，Windows 不在已验证范围内。

## 五项交付在哪里

| 老师要求 | 交付入口 |
|---|---|
| 源码与测试 | [源码](src/guarded_harness/)、[六个测试文件](tests/) |
| `pytest -q` 实际结果 | [测试输出](records/run-final-verified/pytest_output.txt) |
| 至少一条完整运行记录 | [最终版本独立重跑](records/run-final-verified/run_record.md) |
| 一份审计记录 | [审计日志](records/run-final-verified/journal.jsonl) |
| 一份 Checkpoint（检查点）恢复记录 | [独立进程操作与恢复说明](records/run-final-verified/run_record.md)、[最终存档](records/run-final-verified/checkpoint_final.json)、[原始进程输出](records/run-final-verified/process_logs/) |

主要证据还包含 [源码指纹清单](records/run-final-verified/source_manifest.json)、[机器可读核对结果](records/run-final-verified/verification.json)、[修复差异](records/run-final-verified/diff.patch)、[交付说明](records/run-final-verified/result.md)。

## 19 项验收对照

测试列给出文件与代表性测试名，可用 `uv run pytest <文件> -q -k <测试名>` 定向运行；完整验收应运行全量测试。

| 编号 | 验收点 | 测试文件与代表性测试 / 证据 |
|---|---|---|
| G1 | 缺字段、类型错时不执行 handler（实际处理函数） | `test_gate.py`：`test_g1_handler_never_called_on_rejection`、`test_missing_field_rejected_with_field_name` |
| G2 | 第三次连续同调用执行前拦截 | `test_gate.py`：`test_third_identical_call_intercepted_handler_runs_twice` |
| G3 | 未批准写入挂起，文件不变 | `test_harness_loop.py`：`test_write_suspends_no_further_model_request` |
| G4 | 已批准调用不重复执行 | `test_harness_loop.py`：`test_repeated_approval_rejected_no_second_write`、`test_cli_decision_is_once_across_invocations`；`test_gate.py`：`test_duplicate_call_submission_rejected_after_consume` |
| S1 | `artifacts/../../.env` 路径逃逸被拒 | `test_sandbox.py`：`test_artifacts_dotdot_escape_rejected`、`test_escape_target_untouched` |
| S2 | 软链接逃逸、敏感目录拒绝并审计 | `test_sandbox.py`：`test_symlink_escape_rejected_by_realpath`、`test_sensitive_path_rejected`；`test_audit.py`：`test_sandbox_rejection_audited` |
| S3 | 不执行配置外命令 | `test_sandbox.py`：`test_command_not_in_allowlist_rejected_without_execution` |
| S4 | 超时/取消真实终止进程组 | `test_sandbox.py`：`test_timeout_kills_whole_group`、`test_cancel_kills_whole_group` |
| C1 | 安全步骤保存必要状态 | `test_state_checkpoint.py`：`test_snapshot_contains_six_essentials`；`test_harness_loop.py`：`test_sequence_increases_across_loop_and_reload` |
| C2 | 写后结果未保存，不自动重放 | `test_harness_loop.py`：`test_real_process_exit_after_write_before_result` |
| C3 | 换进程恢复、计数接续、不重复写 | `test_state_checkpoint.py`：`test_subprocess_save_then_restore_constraints`；`test_harness_loop.py`：`test_pause_in_subprocess_resume_here`；主要运行记录五段独立进程 |
| C4 | 工作区变化阻止恢复 | `test_harness_loop.py`：`test_workspace_changed_after_checkpoint_blocks` |
| A1 | 拒绝记录含 rule/decision/actor/toolCallId | `test_audit.py`：`test_rejected_call_has_required_fields`；代码字段名为 `tool_call_id` |
| A2 | 完整任务可关联五类事件 | `test_harness_loop.py`：`test_full_run_audits_chain_across_five_stages`；主要审计日志 |
| H1 | 无交付物不能完成 | `test_completion.py`：`test_final_answer_rejected_loop_continues`、`test_fix_requires_configured_tests_and_deliverable` |
| H2 | 修改后旧测试证据失效 | `test_completion.py`：`test_write_after_pass_forces_rerun_end_to_end`、`test_config_change_invalidates_test_evidence` |
| H3 | 达到轮数或预算明确停止 | `test_harness_loop.py`：`test_max_turns_stops_without_fallback_final_answer`、`test_token_budget_exhausted`、`test_write_budget_exhausted_after_refusal` |
| T1 | 全部 P0 确定性测试重复通过 | 全量六文件，共 101 用例，包含 14 项 P0 的覆盖及加固场景 |
| T2 | 真实模型任务证据相互印证 | [真实模型运行记录](records/run-final-verified/run_record.md)：修复任务完成，独立复跑 7/7 通过、差异、交付物、审计及恢复记录齐备 |

六文件均位于 `tests/`：`test_gate.py`、`test_sandbox.py`、`test_state_checkpoint.py`、`test_audit.py`、`test_harness_loop.py`、`test_completion.py`。

## 主要真实运行结果

主要记录来自新临时项目的 DeepSeek `deepseek-chat`：

| 检查项 | 实际结果 |
|---|---|
| 安装与作业测试 | 按锁文件安装，101 项通过 |
| 原样例基线 | 7 用例，4 通过、3 失败 |
| 审批与恢复 | 五段独立进程，两次提案核对、批准、恢复 |
| 最终状态 | COMPLETED；step=10、seq=10、写入2次 |
| 独立复跑 | 修复工作区 7/7 通过 |
| 文件变更 | 原有文件仅 `src/auth/session-policy.ts` 改变；原测试、入口与锁文件未变；另生成 `artifacts/result.md` |
| 证据完整性 | 运行时源码指纹已保存；存档校验和、测试及工作区指纹核对通过 |

该次批准由主代理核对提案后，在用户授权的验收任务中代为执行，记录 `by=codex-user-authorized-review`，不是用户逐条手工批准，也不是模型自行批准。

[代码理解运行记录](records/run-understand-verified/run_record.md)补充第二类真实任务：七段独立进程，两次拒绝错误文档路径、一次批准，完成后原有源码零修改。该记录含独立源码指纹、审计、存档及过程输出。

## 亲自复现真实任务

需要 Node.js（已验证要求 ≥22.19）、npm、可用模型服务及密钥；模型调用会产生服务费用。以下命令均在 `guarded_harness/` 内执行。

```bash
(cd samples/2_bug_fix/demo-app && npm ci --no-audit --no-fund)
(cd samples/2_bug_fix/demo-app && npm test)
# 预期退出码1：4通过、3失败。这是待修复基线，不是作业测试失败。
```

在本机环境中设置 `DEEPSEEK_API_KEY` 和 `DEEPSEEK_BASE_URL`（如 `https://api.deepseek.com`），不要写入提交文件。然后使用一个未占用的运行目录：

```bash
uv run python -m guarded_harness run \
  --mode fix --goal-file samples/2_bug_fix/task_fix.json \
  --source samples/2_bug_fix/demo-app --run-dir .scratch/teacher-fix \
  --artifact-target artifacts/result.md --test-command 'default=npm|test' \
  --protected tests/login-flow.test.ts --protected tests/session-boundary.test.ts \
  --protected tests/session-policy.test.ts --protected package.json --protected package-lock.json \
  --max-turns 20 --max-writes 6 --max-tokens 250000 --base-url-env DEEPSEEK_BASE_URL
```

通常退出码 `3`，状态为 `WAITING_APPROVAL`。查看 `.scratch/teacher-fix/state/checkpoint.json` 的 `pending_approvals`，核对目标路径与完整写入内容；将下面的 `提案ID` 替换为当前实际值：

```bash
uv run python -m guarded_harness approve .scratch/teacher-fix 提案ID
uv run python -m guarded_harness resume .scratch/teacher-fix
```

也可用 `reject` 替代 `approve` 后再恢复。每次命令是独立进程；若再次待审批，重新核对新提案并重复操作。最终 `resume` 退出码 `0` 且状态为 `COMPLETED`，再独立检查：

以下目录由前面的 `--run-dir .scratch/teacher-fix` 复现命令自动生成，仅在完成上述真实任务复现后使用，**无需随作业提交**。如果只核查已提交的运行证据，请直接查看 [records/run-final-verified](records/run-final-verified/run_record.md)，不需要创建该目录。

```bash
(cd .scratch/teacher-fix/workspace && npm test)
cat .scratch/teacher-fix/workspace/artifacts/result.md
```

应测试全过且说明非空。审计和恢复存档位于运行目录的 `audit/`、`state/`。真实模型轮数、调用顺序和提案 ID 不固定；预算耗尽、模型错误或需要核对时不能按完成处理。

## 验证边界

- 自动化测试验证当前实现，包括 C2 结果未知状态的持久化与重复恢复。真实模型记录验证两类任务的运行、审批和恢复流程；安全反例主要由确定性测试验证。
- 真实模型记录的运行时源码身份见各自 source_manifest.json；两份记录早于 C2 持久标记完善，其指纹不代表当前全部源码。C2 的 UNKNOWN_RESULT 标记由真实子进程崩溃测试验证，不以正常任务运行记录替代。
- C2 使用真实子进程写入后 `exit(137)`，覆盖结果未保存窗口；不是任意时刻强杀或系统掉电的完整保证。
- 主要修复运行未新增 sample 测试，复用原有七个测试；模型说明中的“测试补充”标题不作为新增测试事实。
- 路径检查和进程组管理不等于系统级任意代码隔离。临时工作区可能被清理，归档保留原始输出、最终存档、差异与源码指纹。
- 干净目录依赖安装使用了本机 uv 缓存；无缓存下载、Windows 和其他模型供应商未验证。

本轮首轮启动出现依赖未就绪的环境错误，未作为成功证据；失败原始输出与后续全新运行的成功输出分别归档，详见主要运行记录。

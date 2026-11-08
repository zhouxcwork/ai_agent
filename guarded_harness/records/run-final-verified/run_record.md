# 最终版本独立验收记录

日期：2026-11-08。该记录由主代理在新临时项目中实际运行产生，覆盖最新三项边界修复；旧 records 已删除，本目录为本次提交的真实运行证据。

## 环境与代码身份

- macOS arm64；uv 管理的 Python 3.12；smolagents 1.26.0。
- 临时目录：`/var/folders/bp/j7pg2nsj6p57l15ybdmlf7_c0000gn/T/gh-current-bc4aw123`。
- 复制 src、tests、samples、pyproject.toml、uv.lock、README.md 和 .python-version，排除旧虚拟环境、node_modules、缓存及参考源码。
- `source_manifest.json` 保存运行前源码、测试、依赖声明及锁文件的 SHA-256；运行后逐项与当前交付目录核对一致。文档和 records 不纳入源码指纹。
- 密钥仅从约定环境文件读取并注入子进程，未写入记录。归档内容已扫描，无该环境文件中的长凭据值。

## 安装与测试

在临时项目执行：

```bash
uv sync --locked --extra dev
uv run python -m pytest -q
cd samples/2_bug_fix/demo-app
npm ci --no-audit --no-fund
npm test
```

实际：作业测试 **101 passed in 3.37s**；sample 基线 **7 测试、4 通过、3 失败**。安装及测试原始输出见本目录对应文件。Python 安装使用本机 uv 全局缓存，不表示已验证无缓存联网下载。

## 真实模型与跨进程恢复

模型为 DeepSeek `deepseek-chat`，配置见 `run_config.json`。在临时项目运行：

```bash
uv run python -m guarded_harness run \
  --mode fix --goal-file samples/2_bug_fix/task_fix.json \
  --source samples/2_bug_fix/demo-app --run-dir runs/fix-final \
  --artifact-target artifacts/result.md --test-command 'default=npm|test' \
  --protected tests/login-flow.test.ts --protected tests/session-boundary.test.ts \
  --protected tests/session-policy.test.ts --protected package.json --protected package-lock.json \
  --max-turns 20 --max-writes 6 --max-tokens 250000 --base-url-env DEEPSEEK_BASE_URL
# exit 3，step 5，修复提案 prop-7415269ac61d

uv run python -m guarded_harness approve runs/fix-final prop-7415269ac61d --by codex-user-authorized-review
# exit 0
uv run python -m guarded_harness resume runs/fix-final
# exit 3，step 8，交付说明提案 prop-e25565f46ba4

uv run python -m guarded_harness approve runs/fix-final prop-e25565f46ba4 --by codex-user-authorized-review
# exit 0
uv run python -m guarded_harness resume runs/fix-final
# exit 0，COMPLETED，step 10
```

五段命令各在独立进程执行。批准由执行本次验收的主代理核对提案后代用户发出，记录 actor=user、by=codex-user-authorized-review；并非用户逐条手工批准。模型没有审批入口。原始过程输出见 `process_logs/`。

## 独立验收结果

- 在修复工作区另行执行 npm test：**7/7 通过**，退出码 0；输出见 `test_output_after_fix.txt`。
- 原 sample 仍 **4 通过、3 失败**，退出码 1；输出见 `baseline_test_output.txt`。
- 对比基线原有文件：仅 `src/auth/session-policy.ts` 改变，测试、package.json、锁文件均未变；见 `diff.patch`。
- 交付说明 `result.md` 非空（1312 字节），与实际工作区产物相同。
- 最终 checkpoint：COMPLETED / COMPLETED，seq=10、step=10、writes_used=2、tokens_used=31784；plan 非空，校验和重算通过。
- 当前工作区恢复指纹、最新通过测试证据指纹均一致；机器可读核对结果见 `verification.json`。
- 本轮未新增 sample 测试；模型说明中的“测试补充”标题不能当作新增测试的事实。实际为修复源码并复用原有七个测试完成验证。

本次覆盖一类真实任务与完整审批/恢复流程；不把此记录解释为所有安全场景均由真实模型触发。临时工作区可能被系统清理，归档保留原始输出、差异、最终存档与源码指纹。


## 本轮执行与说明边界

- 首轮启动时样例依赖安装与模型运行发生重叠，复制出的工作区缺少 tsx。该轮未完成，不计入成功结果；原始失败日志另存 failed-setup-logs/，记录调用产生的真实环境错误。安装完成后另建全新运行目录重跑，以上成功证据仅对应重跑。
- 本轮模型说明 result.md 中的代码块是简化示意，不是逐字源码；实际源码以 diff.patch 为准（包含无效时间保护逻辑）。说明中的“新增/现有”不证明新增了测试，独立比对确认原七个测试未变。
- 代码理解的历史运行记录已移除；当前代码理解与人工拒绝路径由确定性测试覆盖。本轮真实模型仅重跑测试修复任务，满足 T2 至少一类任务的要求，不声称真实模型重验两类任务。

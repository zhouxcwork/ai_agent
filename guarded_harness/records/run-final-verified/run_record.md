# 最终版本独立验收记录

日期：2026-11-08。该记录由主代理在新临时项目中实际运行产生，作为当前版本优先验收证据；既有运行记录保留为补充。

## 环境与代码身份

- macOS arm64；uv 管理的 Python 3.12；smolagents 1.26.0。
- 临时目录：`/var/folders/bp/j7pg2nsj6p57l15ybdmlf7_c0000gn/T/gh-final-4sycnadh`。
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

实际：作业测试 **94 passed in 3.30s**；sample 基线 **7 测试、4 通过、3 失败**。安装及测试原始输出见本目录对应文件。Python 安装使用本机 uv 全局缓存，不表示已验证无缓存联网下载。

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
# exit 3，step 5，修复提案 prop-75436112de1c

uv run python -m guarded_harness approve runs/fix-final prop-75436112de1c --by codex-user-authorized-review
# exit 0
uv run python -m guarded_harness resume runs/fix-final
# exit 3，step 9，交付说明提案 prop-b14a86915f22

uv run python -m guarded_harness approve runs/fix-final prop-b14a86915f22 --by codex-user-authorized-review
# exit 0
uv run python -m guarded_harness resume runs/fix-final
# exit 0，COMPLETED，step 11
```

五段命令各在独立进程执行。批准由执行本次验收的主代理核对提案后代用户发出，记录 actor=user、by=codex-user-authorized-review；并非用户逐条手工批准。模型没有审批入口。原始过程输出见 `process_logs/`。

## 独立验收结果

- 在修复工作区另行执行 npm test：**7/7 通过**，退出码 0；输出见 `test_output_after_fix.txt`。
- 原 sample 仍 **4 通过、3 失败**，退出码 1；输出见 `baseline_test_output.txt`。
- 对比基线原有文件：仅 `src/auth/session-policy.ts` 改变，测试、package.json、锁文件均未变；见 `diff.patch`。
- 交付说明 `result.md` 非空（1851 字节），与实际工作区产物相同。
- 最终 checkpoint：COMPLETED / COMPLETED，seq=11、step=11、writes_used=2、tokens_used=35250；plan 非空，校验和重算通过。
- 当前工作区恢复指纹、最新通过测试证据指纹均一致；机器可读核对结果见 `verification.json`。
- 本轮未新增 sample 测试；模型说明中的“测试补充”标题不能当作新增测试的事实。实际为修复源码并复用原有七个测试完成验证。

本次覆盖一类真实任务与完整审批/恢复流程；不把此记录解释为所有安全场景均由真实模型触发。临时工作区可能被系统清理，归档保留原始输出、差异、最终存档与源码指纹。

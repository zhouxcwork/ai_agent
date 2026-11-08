# Guarded Harness：可恢复的代码任务执行框架

> **作业三 · 老师验收入口**
>
> 请先阅读 [作业验收指南](ACCEPTANCE.md)：包含快速测试、19 项验收对照、真实运行、审计与恢复证据。
> 当前确定性测试 **101 项通过**。当前真实模型修复任务经两次审批和跨进程恢复后完成，独立复跑被测项目 **7/7 通过**，已覆盖最新三项边界修复。
> [查看完整记录与源码指纹](records/run-final-verified/run_record.md)。

基于固定依赖 `smolagents[openai]==1.26.0`，为 `ToolCallingAgent` 增加 Guarded Harness（受控执行框架），不修改其核心源码。模型选择工具和提出完成申请；程序负责参数门禁、执行边界、审批、检查点、审计、恢复和最终验收。

支持两类任务：**代码理解**（源码只读，生成说明文档）与**测试修复**（审批后写入，运行配置内测试，以当前证据判定完成）。

## 安装与快速开始

已验证环境：macOS arm64、Python 3.12.13、uv（Python 项目管理工具）。完整依赖由 `uv.lock` 锁定。

```bash
# 从 homework 仓库根目录进入作业项目
cd guarded_harness
uv sync --locked --extra dev
uv run python -m pytest -q
```

测试不需要模型密钥、Node.js 或真实模型请求；首次安装依赖可能需要网络。测试范围仅含本作业六个指定测试文件，不包含 smolagents 上游测试。

## 架构与执行流程

```text
模型提出工具调用
  → 串行调度 → 参数门禁 → 路径/命令边界 → 执行或等待审批
  → 审计与检查点 → 模型读取结果继续决策
  → 完成申请 → 当前测试证据与交付物检查 → 完成或驳回
```

| 模块 | 职责 |
|---|---|
| `types.py` | 状态、停止原因、证据、提案和检查点数据结构 |
| `gate.py` | 参数结构校验、连续重复调用拦截 |
| `sandbox.py` | 路径及软链接校验、敏感/受保护文件、命令白名单、进程组终止 |
| `approval.py` | 提案登记、人工决定、一次性消费 |
| `tools.py` | 受控 `read_file`、`write_file`、`run_test` 工具 |
| `audit.py` | 追加写入 JSONL（逐行 JSON）审计日志 |
| `state.py` | 检查点原子保存、会话还原、执行日志与存档对齐 |
| `completion.py` | 最新测试结果、内容指纹及非空交付物检查 |
| `loop.py` | Agent 串行适配、预算、审批代执行与恢复编排 |
| `__main__.py` | 命令行 `run / approve / reject / resume` |

通过子类扩展 `ToolCallingAgent` 的工具调度与执行入口，关联调用 ID。外层消费流式运行至安全步骤边界；等待审批或预算耗尽后停止推进。

## 运行真实任务

测试修复样例需要 Node.js（已验证版本要求 ≥22.19）、npm 和模型服务。先恢复样例依赖：

```bash
(cd samples/2_bug_fix/demo-app && npm ci --no-audit --no-fund)
```

通过环境变量设置 `DEEPSEEK_API_KEY` 和 `DEEPSEEK_BASE_URL`（例如 `https://api.deepseek.com`），不要将真实值写入配置或提交记录。

完整的修复、审批和恢复操作见 [验收指南](ACCEPTANCE.md#亲自复现真实任务)。代码理解示例：

```bash
uv run python -m guarded_harness run --mode understand \
  --goal-file samples/2_bug_fix/task_understand.json \
  --source samples/1_login_demo --run-dir .scratch/understand-demo \
  --artifact-target artifacts/login-logic.md --base-url-env DEEPSEEK_BASE_URL
```

运行目录必须是新目录。模型提出写入后，先核对提案，再执行 `approve` 或 `reject`，随后 `resume`；批准本身不执行写入。

| 退出码 | 含义 |
|---|---|
| `0` | 运行完成，或审批决定成功登记；需结合子命令判断 |
| `3` | 等待审批或运行暂停 |
| `1` | 失败、取消或恢复被拒绝 |
| `2` | 命令行参数解析错误 |

## 状态、存储与边界

运行状态为 `RUNNING`（执行中）、`WAITING_APPROVAL`（待审批）、`PAUSED`（暂停），以及 `COMPLETED / FAILED / CANCELLED`（完成/失败/取消终态）。终态不续跑；模型错误可恢复；预算耗尽不自动提升；`NEEDS_REVIEW`（需要核对）不自动绕过。

每次运行独立保存：

```text
运行目录/
├── run_config.json
├── workspace/              # 从 samples 复制的被测项目
│   └── artifacts/          # 交付说明
├── state/checkpoint.json   # 检查点
└── audit/journal.jsonl     # 审计日志
```

- 写操作先审批，执行意图落盘后才写入；结果不明时停止核对，不自动重放。
- 修复任务必须配置测试命令，并生成指定非空交付物（默认 `artifacts/result.md`）。默认指纹覆盖工作区全部普通文件（包含根目录源码），排除依赖、缓存、运行记录及交付产物；受测内容变化会使旧测试证据失效。
- `samples/` 保持课程原始状态：修复样例天然为 4 通过、3 失败；所有修复发生在运行副本。
- 执行边界提供路径检查、命令白名单与进程组管理，**不是系统级任意代码隔离**；样例及配置命令受信任。
- 不承诺任意崩溃情况下副作用恰好一次；不确定结果交由核对。
- 当前进程组实现面向 POSIX（类 Unix 系统）；Windows 未验证。

## 文档与运行证据

- [老师验收指南](ACCEPTANCE.md)：提交清单、验收映射和复现步骤。
- [当前源码独立重跑](records/run-final-verified/run_record.md)：主要验收证据，含原始输出与源码指纹。
- [课程作业原文](<../assignments/AI Agent 全栈工程师训练营-作业三.md>)。

`records/` 只保留本轮独立重跑记录；真实模型验证测试修复任务，代码理解及拒绝反馈由确定性测试覆盖。

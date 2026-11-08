# T2 运行记录：代码理解任务（真模型，run-understand-1，当前修订版重验）

> 由独立复核修订后的当前版本产生；修订前旧记录已移除。本任务额外实证 **人工拒绝（reject）**
> 反馈路径——与 [最新独立修复运行](../run-final-verified/run_record.md) 的全批准路径互补。

## 环境

2026-11-08；macOS arm64；Python 3.12.13；smolagents 1.26.0；DeepSeek `deepseek-chat`，通过 OpenAI 兼容接口接入。密钥经 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` 环境变量注入，不落盘、不打印。

- 被测项目：`samples/1_login_demo`（TS 登录服务，只读；写范围限定 `artifacts/**`）
- 任务：`samples/2_bug_fix/task_understand.json`；约定产物 `artifacts/login-logic.md`

## 命令与退出码（五段独立进程，含一次人工拒绝）

```bash
# ① 驱动至产物提案（模型读了 11 个源文件后请求写 artifacts/login.md——路径不符约定）
uv run python -m guarded_harness run \
  --mode understand --goal-file samples/2_bug_fix/task_understand.json \
  --source samples/1_login_demo --run-dir .scratch/t2-und-1 \
  --artifact-target artifacts/login-logic.md \
  --max-turns 14 --max-writes 3 --max-tokens 200000 --base-url-env DEEPSEEK_BASE_URL
# 退出码 3：WAITING_APPROVAL step=6 pending=['prop-60f8c6e5bf3a']（artifacts/login.md）

# ② 人工拒绝（独立进程持久化）→ ③ 恢复：模型收到拒绝理由，改写约定路径，再次挂起
uv run python -m guarded_harness reject .scratch/t2-und-1 prop-60f8c6e5bf3a
uv run python -m guarded_harness resume .scratch/t2-und-1   # 退出码 3：step=8 pending=['prop-f74368a792bc']

# ④ 批准 → ⑤ 恢复至完成
uv run python -m guarded_harness approve .scratch/t2-und-1 prop-f74368a792bc
uv run python -m guarded_harness resume  .scratch/t2-und-1   # 退出码 0：COMPLETED
```

## 状态与预算

- 终态：`COMPLETED / COMPLETED`；step=11、writes_used=1、tokens_used=54637（上限 14/3/200000）
- 被拒绝的 `artifacts/login.md` **未写入磁盘**（拒绝不产生副作用）；唯一写入为约定产物
  `artifacts/login-logic.md`（5431 字节）

## 程序侧把关的真实体现（审计链 31 条）

```
step0  sandbox.cmd_not_allowed REJECT      ← 任务未配测试命令，run_test 越权被白名单拒绝
step1-3 tool.read_file ×11  ALLOW/EXECUTED ← 源码阅读（含 source_read 证据）
step4  sandbox.write_out_of_scope REJECT   ← 模型一度试图写界外路径，被 understand 写范围拒绝
step5  approval.pending WAIT（login.md）   ← 路径不符约定
step6  approval.rejected REJECT (user)     ← 人工拒绝（actor=user，不可由模型伪造）
step7  approval.pending WAIT（login-logic.md）← 模型按拒绝理由改写约定路径
…     approved → INTENT → write.result
终态   run.stopped STOP COMPLETED
```

## 独立验证

1. `diff -r --exclude=artifacts samples/1_login_demo workspace` → 零差异（源码只读未被触碰）
2. 产物 `login-logic.md`（5431 字节，随记录交付）描述的路由→服务→仓储分层与文件清单
   与 `samples/1_login_demo/src/` 实际结构一致
3. `checkpoint_final.json` checksum 重算通过；记录经密钥扫描（仅环境变量名）

## 交付文件

`journal.jsonl` · `checkpoint_final.json` · `run_config.json` · `login-logic.md`

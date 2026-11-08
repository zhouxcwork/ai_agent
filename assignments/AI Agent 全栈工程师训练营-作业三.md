# AI Agent 全栈工程师训练营\-作业三

# 基于 smolagents 实现 Guarded Harness

## 作业问题

> 在不重写 smolagents 核心的前提下，为 `ToolCallingAgent` 增加一层可恢复的 Guarded Coding Harness。模型继续负责决策，程序负责门禁、执行边界、状态、审计、恢复和最终验收，使其能够安全完成“代码理解”和“测试修复”两类仓库任务。
>
> smolagents github：https://github\.com/huggingface/smolagents

## 必须交付

测试文件：

```Plain
tests/test_gate.py
tests/test_sandbox.py
tests/test_state_checkpoint.py
tests/test_audit.py
tests/test_harness_loop.py
tests/test_completion.py
```

## 核心验收



| 编号 | 验收场景                      | 通过标准                                         | 优先级 |
| ---- | ----------------------------- | ------------------------------------------------ | ------ |
| G1   | 参数缺字段或类型错误          | 返回明确错误，handler 调用次数为 0               | P0     |
| G2   | 连续三次相同工具和参数        | 第三次在执行前拦截，handler 只执行两次           | P0     |
| G3   | 未审批的写操作                | 进入 WAITING\_APPROVAL，目标文件不变             | P0     |
| G4   | 审批后重复执行同一调用        | 第一次只执行一次，第二次拒绝，不重复写入         | P0     |
| S1   | artifacts/../../.env 路径逃逸 | Sandbox 拒绝，handler 不执行                     | P0     |
| S2   | 软链接逃逸或敏感目录访问      | 真实落点校验后拒绝，审计留痕                     | P1     |
| S3   | run\_test 配置外命令          | 命令不在允许范围时不执行                         | P0     |
| S4   | 命令超时或用户取消            | 进程组真实终止，不是只停止等待                   | P1     |
| C1   | 每个安全步骤后保存 Checkpoint | 包含 run、step、计划、证据、预算和待审批动作     | P0     |
| C2   | 写入完成后结果未保存时恢复    | 标记 UNKNOWN\_RESULT，不得自动重放               | P0     |
| C3   | 正常中断后换进程恢复          | 找回进度，已完成写入不重复，计数不清零           | P0     |
| C4   | 恢复时工作区已变化            | 旧证据或旧审批失效，停止并等待核对               | P0     |
| A1   | 查看一次被拒绝的调用          | 审计记录含 rule、decision、actor、toolCallId     | P0     |
| A2   | 查看一次完整任务              | 能关联 Gate、Sandbox、State、Approval 和执行结果 | P1     |
| H1   | 模型说完成但无交付物          | final answer 被拒绝，不能进入完成态              | P0     |
| H2   | 测试通过后又修改源码          | 旧测试证据失效，必须重跑当前版本                 | P0     |
| H3   | 达到最大轮数或预算            | 以明确 StopReason 停止，不伪装成功               | P0     |
| T1   | 运行完整测试集                | 所有 P0 场景都有确定性测试，且可重复通过         | P0     |
| T2   | 用真实模型跑通一类任务        | 产物、Diff、测试记录和审计能相互印证             | P1     |


## 最后提交

- 源码与测试。
- `pytest -q` 实际结果。
- 至少一条完整运行记录。
- 一份审计记录。
- 一份 Checkpoint 恢复记录。

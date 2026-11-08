"""作业三：基于 smolagents 的可恢复 Guarded Coding Harness。

模型负责决策；程序负责门禁、执行边界、状态、审计、恢复与最终验收。
分层：types(协议词汇) / gate(门禁) / sandbox(沙箱) / approval(审批) /
tools(受控工具) / audit(审计) / state(检查点) / completion(完成判定) / loop(主循环)。
"""

__version__ = "0.1.0"

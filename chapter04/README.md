# 作业四 · 项目长期记忆与混合检索

两题分别验证项目知识的跨会话复用，以及登录资料变化后检索证据的更新。各题的 README 包含项目结构、运行环境、复跑步骤和验收材料。

| 题目 | 实现与验收 | 入口 |
| --- | --- | --- |
| 第一题 · pi 项目长期记忆 | 将测试入口、命令和来源写入项目内的 Markdown 记忆。全新会话回读，迁移测试后更新原记录，并验证删除和项目隔离。 | [源码与复跑](q1-memory/README.md) · [验收证据](evidence/q1/) |
| 第二题 · 登录资料混合检索 | 同批分块建立向量与 BM25（关键词相关性）索引，用 RRF（倒数排名融合）汇总结果，验证登录时长从 30 分钟更新到 20 分钟后答案和引用同步变化。 | [源码与复跑](q2-rag/README.md) · [验收证据](evidence/q2/) |

[课程作业四原文](<../assignments/AI Agent 全栈工程师训练营-作业四.md>)

## 离线测试

从本目录分别执行两组命令。安装依赖后，离线测试无需模型凭据。

第一题验证记忆读写与跨进程回读，并运行登录应用测试。

```bash
cd q1-memory/login_demo
npm ci
npm run test:memory
npm run build
python -m unittest -v test_session.py
```

第二题验证索引回读、旧分块退出、排名融合和引用编号校验。

```bash
cd q2-rag
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
```

真实模型复跑的凭据配置与命令见各题 README。两题的截图、运行日志和落盘证据保存在各自的证据目录。

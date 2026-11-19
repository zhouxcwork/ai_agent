# 作业四第二题 · 登录资料混合检索

将课程 4-6 示例的登录资料按清单分块。两路索引共享同一批节点，向量索引使用 LlamaIndex 与千问真实文本向量化，BM25 负责中文关键词检索。RRF（倒数排名融合）合并两路排名，DeepSeek Flash 仅根据混合证据生成带引用的回答。

`corpus/` 完整收录课程 4-6 的语料，包括现行资料和历史归档。30 分钟是该资料所描述的初版业务规则。第一题 TypeScript 登录演示使用自己的令牌实现。

## 项目结构与文件说明

```text
chapter04/
├── q2-rag/
│   ├── README.md
│   ├── ingest.py              # 加载、分块并构建两路索引
│   ├── query.py               # 三路检索对比、模型回答和引用
│   ├── update_corpus.py       # 复制语料并把登录时长改为 20 分钟
│   ├── verify_update.py       # 核对两路索引及旧分块退出
│   ├── rag/
│   │   ├── docload.py         # 现行资料清单和文本加载
│   │   ├── store.py           # 生成分块、节点 ID 和向量索引
│   │   ├── hybrid.py          # 中文 BM25、节点落盘和 RRF 融合
│   │   ├── qwen.py            # 千问文本向量化适配器
│   │   └── answer.py          # 引用来源的显示格式
│   ├── corpus/                # 完整课程 4-6 语料，共 12 份
│   │   ├── app/               # 登录会话实现和配置资料
│   │   ├── docs/              # 会话规则、FAQ、测试和对照说明
│   │   ├── tables/            # 会话类型与时长对照表
│   │   ├── history/           # 历史记录
│   │   ├── web/               # 旧版发布说明
│   │   └── pdf/               # 旧版需求文档
│   ├── tests/test_rag.py      # 双路入库、更新、融合和引用校验
│   ├── pyproject.toml         # Python 项目和依赖声明
│   ├── uv.lock                # 依赖锁文件
│   ├── .python-version       # Python 版本
│   └── .env.example          # 环境变量模板
└── evidence/q2/        # 手跑截图、日志和两版落盘索引
```

`corpus/` 沿用课程 4-6 原始资料。`rag/` 复用并调整了示例的文本加载、分块、向量化和融合代码。建库和查询入口负责串起作业流程，语料更新与索引核对脚本用于验证 30 分钟到 20 分钟的变化。复跑时生成 `storage/v1/`、`storage/v2/` 和 `corpus-v2/`。

## 安装与配置

在本目录用 Python 3.13.12 和 uv 安装锁定依赖。

```bash
uv sync --locked
cp .env.example .env
```

在 `.env` 填写 `QWEN_API_KEY` 和 `DEEPSEEK_API_KEY`，模型及端点可按账号配置填写。DeepSeek 生成模型为 `deepseek-flash`。通过 `uv run --env-file .env` 向脚本注入配置。

## 按题面复跑

```bash
# 1. 初版建库，打印节点数及带来源的分块
uv run --locked --env-file .env python ingest.py --corpus corpus --store storage/v1

# 2. 三路各展示前五条并生成带引用回答，预期 30 分钟
uv run --locked --env-file .env python query.py --store storage/v1 --question '登录会话的有效期是多少分钟？'

# 3. 在副本更新现行登录规则，全量重建并比较两版，预期 20 分钟
uv run --locked python update_corpus.py corpus corpus-v2
uv run --locked --env-file .env python ingest.py --corpus corpus-v2 --store storage/v2
uv run --locked --env-file .env python verify_update.py storage/v1 storage/v2
uv run --locked --env-file .env python query.py --store storage/v2 --question '登录会话的有效期是多少分钟？'
```

重复验收时，使用新的语料副本路径和索引目录。更新脚本保留原始语料，只修改现行登录时长涉及的六个文件。资料加载使用固定清单，旧发布说明、旧需求和历史记录随项目交付，入库清单只包含现行资料。支付会话和记住我令牌的期限保持原值。

`ingest.py` 生成向量 JSON、BM25 节点 JSON 和建库清单。查询时校验两路节点 ID 与正文一致。更新验证读取两版实际落盘的索引，确认旧时长分块退出新版。回答中的编号可追溯到混合检索节点，输出同时展示来源与证据正文。对于作业原题和支持的近义问法，DeepSeek Flash 提供候选回答和引用编号，程序根据所引正文中明确的登录会话时长生成最终结论。证据缺失、冲突或仅支持支付会话时长时，查询会报错。

## 离线回归

```bash
uv run --locked python -m unittest discover -s tests -v
```

用测试向量验证双路建库、回读、旧分块退出、RRF 排名、BM25 零分排除、引用编号和登录时长证据校验。真实模型输出见验收材料。

## 手动验收材料

截图与日志中的 `test-02/` 是手跑时使用的临时项目副本。提交时保留 `q2-rag/` 和 `evidence/q2/` 的相对位置即可。手跑副本内的本地环境、依赖缓存和临时语料由上述命令重建。

| 题面步骤 | 实际结果 | 证据 |
| --- | --- | --- |
| 初版建库 | 两路各 21 个节点，展示带来源的分块正文。 | [截图](../evidence/q2/01-ingest.png) · [日志](../evidence/q2/01-ingest.log) |
| 三路查询 | 每路前五条，DeepSeek Flash 回答 30 分钟并引用初版正文。 | [截图](../evidence/q2/02-query.png) · [日志](../evidence/q2/02-query.log) |
| 更新复查 | 重建两路，确认 7 个旧时长分块退出新版，同题回答 20 分钟并引用新版正文。 | [截图](../evidence/q2/03-update.png) · [日志](../evidence/q2/03-update.log) |

证据目录还保留了两版实际落盘的索引及清单。[初版清单](../evidence/q2/storage/v1/manifest.json) · [新版清单](../evidence/q2/storage/v2/manifest.json)

本次手跑使用加固后的查询代码，两版均以实际引用的正文生成结论。原问句查得 30 分钟，更新后查得 20 分钟。截图和日志记录了这两次运行。

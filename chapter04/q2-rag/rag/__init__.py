"""4-6 RAG 服务：千问向量 + BM25 混合检索（基于 LlamaIndex）。

两个入口脚本：
- ingest.py：文档 → 向量库（千问 embedding API）+ BM25 索引
- query.py：带参数查询，混合检索（RRF 融合）+ LLM 生成带引用的回答
"""

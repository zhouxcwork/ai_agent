"""千问文本向量化适配器，复用课程的 LlamaIndex BaseEmbedding 接法。"""

from __future__ import annotations

import os
import time
from typing import List

from llama_index.core.base.embeddings.base import BaseEmbedding
from openai import OpenAI as OpenAIClient


# 计费口径：千问按 token 计费，沿用 4-4 的统计维度（请求次数/文本数/字符数），ingest 结束时打印。
CALL_STATS = {"embed_calls": 0, "embed_texts": 0, "embed_chars": 0, "embed_seconds": 0.0}

_client: OpenAIClient | None = None


def _get_client() -> OpenAIClient:
    """模块级懒单例。LlamaIndex 的 BaseEmbedding 是 pydantic 模型，
    把 client 存成字段要开 arbitrary types，模块级单例更省事。"""
    global _client
    if _client is None:
        _client = OpenAIClient(api_key=os.environ["QWEN_API_KEY"], base_url=os.environ["QWEN_EMBEDDING_BASE_URL"], timeout=45)
    return _client


class QwenEmbedding(BaseEmbedding):
    """千问 qwen3.7-text-embedding-flash，默认 1024 维。

    truncate_dim > 0 时把该值传给服务端原生 dimensions 参数（256/512/768/1024），
    千问原生支持降维，不需要客户端截断 + 重归一化。
    入库与查询共用同一个端点（千问不分 db/query 类型，这点与 BGE 不同）。
    """

    batch_size: int = 8
    truncate_dim: int = 0

    def _embed(self, texts: List[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            extra = {"dimensions": self.truncate_dim} if self.truncate_dim else {}
            t0 = time.time()
            resp = _get_client().embeddings.create(model=os.environ["QWEN_EMBEDDING_MODEL"], input=batch, **extra)
            CALL_STATS["embed_calls"] += 1
            CALL_STATS["embed_texts"] += len(batch)
            CALL_STATS["embed_chars"] += sum(len(t) for t in batch)
            CALL_STATS["embed_seconds"] += time.time() - t0
            out.extend(item.embedding for item in sorted(resp.data, key=lambda item: item.index))
        return out

    def _get_text_embedding(self, text: str) -> List[float]:
        return self._embed([text])[0]

    def _get_text_embeddings(self, texts: List[str]) -> List[List[float]]:
        return self._embed(texts)

    def _get_query_embedding(self, query: str) -> List[float]:
        return self._embed([query])[0]

    async def _aget_query_embedding(self, query: str) -> List[float]:  # pragma: no cover
        return self._get_query_embedding(query)

    async def _aget_text_embedding(self, text: str) -> List[float]:  # pragma: no cover
        return self._get_text_embedding(text)

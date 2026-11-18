"""中文 BM25 与按节点 ID 合并的 RRF。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List, Sequence

import jieba
from llama_index.core.schema import NodeWithScore, TextNode
from rank_bm25 import BM25Okapi

jieba.setLogLevel(60)


# ---------------------------------------------------------------- 分词

def tokenize(text: str) -> List[str]:
    """中文用 jieba 切，英文和代码符号按原样保留。"""
    return [t for t in jieba.lcut(text.lower()) if t.strip()]


# ---------------------------------------------------------------- BM25

class BM25Retriever:
    """关键词检索：不依赖向量，符号名和命令能精确命中。"""

    def __init__(self, nodes: Sequence[TextNode]):
        self.nodes = list(nodes)
        self.corpus = [tokenize(n.text) for n in self.nodes]
        self.bm25 = BM25Okapi(self.corpus)

    def retrieve(self, query: str, top_k: int = 5) -> List[NodeWithScore]:
        scores = self.bm25.get_scores(tokenize(query))
        matched = (i for i, score in enumerate(scores) if score != 0)
        order = sorted(matched, key=lambda i: scores[i], reverse=True)[:top_k]
        return [NodeWithScore(node=self.nodes[i], score=float(scores[i])) for i in order]


def save_bm25_nodes(nodes: Sequence[TextNode], path: Path) -> None:
    """把 BM25 的语料节点落盘，和向量库放在同一个存储目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([node.model_dump() for node in nodes], ensure_ascii=False), encoding="utf-8")


def load_bm25_nodes(path: Path) -> List[TextNode]:
    return [TextNode.model_validate(node) for node in json.loads(path.read_text(encoding="utf-8"))]


# ---------------------------------------------------------------- RRF 融合

def rrf_fuse(result_lists: Iterable[Sequence[NodeWithScore]], top_k: int = 5,
             k: int = 60, weights: Sequence[float] | None = None) -> List[NodeWithScore]:
    """倒数排名融合：只看名次，不看两边的分数。

    RRF 的分数 = Σ weight / (k + rank)，rank 从 1 开始。
    向量分和 BM25 分不在一个量纲上，直接相加没有意义。
    weights 让召回更准的一路占更大比重；不传就是等权。
    """
    lists = list(result_lists)
    if weights is None:
        weights = [1.0] * len(lists)
    fused: dict[str, float] = {}
    keep: dict[str, TextNode] = {}
    for weight, results in zip(weights, lists):
        for rank, item in enumerate(results, 1):
            key = item.node.node_id
            fused[key] = fused.get(key, 0.0) + weight / (k + rank)
            keep.setdefault(key, item.node)
    order = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    return [NodeWithScore(node=keep[key], score=score) for key, score in order]

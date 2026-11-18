"""用 LlamaIndex SimpleVectorStore（JSON）构建并加载向量索引。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import List

from llama_index.core import (
    Settings,
    StorageContext,
    VectorStoreIndex,
    load_index_from_storage,
)
from llama_index.core.node_parser import (
    MarkdownNodeParser,
    SentenceSplitter,
)
from llama_index.core.schema import TextNode
from llama_index.core.vector_stores import SimpleVectorStore

from .qwen import QwenEmbedding

BM25_FILE = "bm25_nodes.json"


def get_embedding() -> QwenEmbedding:
    return QwenEmbedding()


# ---------------------------------------------------------------- 分块（同 4-4：Markdown 按标题切，代码整文件一个节点）

def _heading_of(text: str, header_path: str) -> str:
    """取小节标题：优先用正文第一行的标题，再拼上级路径。"""
    import re

    first = text.strip().splitlines()[0] if text.strip() else ""
    m = re.match(r"^#{1,6}\s+(.*)$", first)
    own = m.group(1).strip() if m else ""
    parents = header_path.strip("/").replace("/", " > ")
    if parents and own:
        return f"{parents} > {own}"
    return own or parents


def _decorate(node: TextNode, doc) -> TextNode:
    header = node.metadata.get("header_path", "") or ""
    node.metadata = {
        **doc.metadata,
        "heading": _heading_of(node.text, header),
        "header_path": header,
    }
    return node


def chunk_documents(docs, chunk_size: int = 384, overlap: int = 0) -> List[TextNode]:
    """Markdown 交给 MarkdownNodeParser 按标题切；代码整文件一个节点。"""
    parser = MarkdownNodeParser()
    inner = SentenceSplitter(chunk_size=chunk_size, chunk_overlap=overlap)
    nodes: List[TextNode] = []
    for doc in docs:
        if doc.metadata.get("doc_type") == "code":
            nodes.append(TextNode(
                text=doc.text,
                metadata={**doc.metadata, "heading": "(整文件)"},
            ))
            continue
        for node in parser.get_nodes_from_documents([doc]):
            for sub in inner.get_nodes_from_documents([node]) or [node]:
                nodes.append(_decorate(sub, doc))
    for position, node in enumerate(nodes):
        content = f"{node.metadata['file_path']}\0{position}\0{node.text}"
        node.id_ = sha256(content.encode()).hexdigest()
    return nodes


# ---------------------------------------------------------------- 建库 / 开库

def build_and_save(nodes: List[TextNode], store_dir: Path, embed_model=None) -> VectorStoreIndex:
    """千问向量化写入 SimpleVectorStore，落盘到 store_dir；nodes 供 BM25 用。"""
    embed_model = embed_model or get_embedding()
    Settings.embed_model = embed_model
    store = SimpleVectorStore()
    index = VectorStoreIndex(
        nodes,
        storage_context=StorageContext.from_defaults(vector_store=store),
        embed_model=embed_model,
        show_progress=True,
    )
    store_dir.mkdir(parents=True, exist_ok=True)
    # 整个存储目录落盘（default__vector_store.json + docstore.json 等）
    index.storage_context.persist(str(store_dir))
    return index


def load_index(store_dir: Path, embed_model=None) -> VectorStoreIndex:
    """从落盘目录重建索引（向量数据直接读，不重新 embedding）。"""
    embed_model = embed_model or get_embedding()
    Settings.embed_model = embed_model
    ctx = StorageContext.from_defaults(persist_dir=str(store_dir))
    return load_index_from_storage(ctx, embed_model=embed_model)


def bm25_path(store_dir: Path) -> Path:
    return store_dir / BM25_FILE

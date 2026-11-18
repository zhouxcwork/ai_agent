"""构建一版可独立加载的双路索引。"""

import argparse
import json
import os
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from rag.docload import load_all
from rag.hybrid import save_bm25_nodes
from rag.store import bm25_path, build_and_save, chunk_documents


def build(corpus: Path, store: Path, embed_model=None):
    if store.exists():
        raise ValueError(f"索引目录已存在，请指定新目录：{store}")
    docs = load_all(corpus)
    nodes = chunk_documents(docs)
    if not nodes:
        raise ValueError("没有可入库的分块")
    sources = {
        doc.id_: sha256((corpus / doc.id_).read_bytes()).hexdigest()
        for doc in docs
    }
    manifest = {
        "version": sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest(),
        "embedding_model": os.environ.get("QWEN_EMBEDDING_MODEL"),
        "sources": sources,
        "node_ids": [node.node_id for node in nodes],
    }
    store.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=store.parent, prefix=".build-") as temporary:
        staging = Path(temporary) / "index"
        build_and_save(nodes, staging, embed_model)
        save_bm25_nodes(nodes, bm25_path(staging))
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        staging.rename(store)
    sample = next((node for node in nodes if "登录会话有效期为" in node.text), nodes[0])
    print(f"索引目录：{store.resolve()}\n语料版本：{manifest['version'][:12]}")
    print(f"向量 / BM25：各 {len(nodes)} 个节点，共用同批分块和节点 ID。")
    print(f"分块样例 [{sample.metadata['file_path']} § {sample.metadata['heading']}]")
    print(sample.text)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    args = parser.parse_args()
    build(args.corpus, args.store)

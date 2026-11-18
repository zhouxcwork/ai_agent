"""并列展示向量、BM25、RRF，并根据混合证据回答。"""

import argparse
import json
import os
import re
from pathlib import Path

from openai import OpenAI

from rag.answer import label
from rag.hybrid import BM25Retriever, load_bm25_nodes, rrf_fuse
from rag.store import bm25_path, load_index

QUESTION = "登录会话的有效期是多少分钟？"


def load_bundle(store: Path, embed_model=None):
    manifest = json.loads((store / "manifest.json").read_text(encoding="utf-8"))
    if embed_model is None and manifest["embedding_model"] != os.environ.get("QWEN_EMBEDDING_MODEL"):
        raise ValueError("当前向量模型与建库模型不一致")
    index = load_index(store, embed_model)
    nodes = load_bm25_nodes(bm25_path(store))
    expected = set(manifest["node_ids"])
    if set(index.vector_store.data.embedding_dict) != expected or {n.node_id for n in nodes} != expected:
        raise ValueError("两路索引的节点 ID 与建库清单不一致")
    if any(index.docstore.get_node(node.node_id).text != node.text for node in nodes):
        raise ValueError("两路索引的节点正文不一致")
    return index, nodes, manifest


def validate_citations(answer: str, count: int) -> list[int]:
    markers = re.findall(r"\[([^\[\]]+)\]", answer)
    if not answer.strip() or not markers or any(
        not marker.isascii() or not marker.isdecimal() or not 1 <= int(marker) <= count
        for marker in markers
    ):
        raise ValueError("模型回答为空或引用无效，引用格式为 [1]、[2]")
    return list(dict.fromkeys(map(int, markers)))


def generate(question, hits):
    evidence = "\n\n".join(f"[{i}] {label(hit)}\n{hit.node.text}" for i, hit in enumerate(hits, 1))
    prompt = (
        "只根据证据回答问题，引用使用 [1] 这样的编号。"
        "优先使用明确写明登录会话有效期的规则，不要把支付会话或记住我令牌的期限当成登录时长。"
        "给出简洁中文回答。\n\n"
        f"问题：{question}\n\n证据：\n{evidence}"
    )
    client = OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        timeout=60,
    )
    response = client.chat.completions.create(
        model="deepseek-flash",
        messages=[{"role": "user", "content": prompt}],
        extra_body={"thinking": {"type": "disabled"}},
    )
    return response.choices[0].message.content or ""


def query(store: Path, question: str):
    index, nodes, manifest = load_bundle(store)
    vector = index.as_retriever(similarity_top_k=20).retrieve(question)
    keyword = BM25Retriever(nodes).retrieve(question, top_k=20)
    mixed = rrf_fuse([vector, keyword], top_k=5)
    print(f"问题：{question}\n索引：{store.resolve()} · {manifest['version'][:12]}")
    for name, hits in [("向量", vector), ("BM25", keyword), ("RRF 混合", mixed)]:
        print(f"\n{name}前 5 条（实际展示 {min(5, len(hits))} 条）")
        for number, hit in enumerate(hits[:5], 1):
            summary = " ".join(hit.node.text.split())[:85]
            print(f"{number}. {label(hit)} | {summary}")
    answer = generate(question, mixed)
    citations = validate_citations(answer, len(mixed))
    print(f"\n回答（DeepSeek Flash）：{answer}\n")
    for number in citations:
        hit = mixed[number - 1]
        print(f"[{number}] {label(hit)}\n{hit.node.text}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--question", default=QUESTION)
    args = parser.parse_args()
    query(args.store, args.question)

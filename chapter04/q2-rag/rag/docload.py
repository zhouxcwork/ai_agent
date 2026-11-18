"""按固定清单加载当前登录资料，复用课程文本清洗。"""

import csv
import io
import re
from pathlib import Path
from typing import List

from llama_index.core import Document

EXT_TO_TYPE = {".md": "doc", ".py": "code", ".csv": "table"}

def clean_text(text: str) -> str:
    """去掉解析残留：页码注释、连续空行、行尾空格、全角空格。"""
    text = re.sub(r"<!--\s*page\s*\d+\s*-->", "", text)
    text = text.replace("\u3000", " ")
    lines = [line.rstrip() for line in text.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------- 各格式解析

def parse_front_matter(text: str) -> tuple[dict, str]:
    """把 Markdown 顶部的 YAML 头信息解析成元数据，正文里不留这段。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n")
    if len(parts) < 2:
        return {}, text
    end = None
    for i in range(1, len(parts)):
        if parts[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    meta: dict = {}
    for line in parts[1:end]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    body = "\n".join(parts[end + 1:]).lstrip("\n")
    return meta, body


def load_table(path: Path) -> str:
    rows = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8")))
    return "\n".join("；".join(f"{key}是{value}" for key, value in row.items() if value) + "。"
                     for row in rows)


CURRENT_FILES = (
    "app/auth.py", "app/config.py", "app/session.py",
    "docs/faq.md", "docs/payment-session.md", "docs/session-rules.md",
    "docs/testing-guide.md", "tables/session-matrix.csv",
)


def load_all(corpus: Path) -> List[Document]:
    docs = []
    for source in CURRENT_FILES:
        path = corpus / source
        metadata = {"file_path": source, "doc_type": EXT_TO_TYPE[path.suffix]}
        if path.suffix == ".csv":
            body = load_table(path)
        else:
            extra, body = parse_front_matter(path.read_text(encoding="utf-8"))
            metadata = {**extra, **metadata}
        docs.append(Document(text=clean_text(body), metadata=metadata, id_=source))
    return docs

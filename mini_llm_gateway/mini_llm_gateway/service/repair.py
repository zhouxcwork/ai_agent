from __future__ import annotations

import re

_CODE_BLOCK = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def extract_json(content: str) -> str:
    """无损提取修复（ADR 0009）：markdown 代码块 → 首 { 到尾 } 截取 → 去首尾空白。
    不改动任何字符语义，只剥壳；无可提取内容时原样返回。
    """
    stripped = content.strip()
    match = _CODE_BLOCK.search(stripped)
    if match:
        return match.group(1).strip()
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first != -1 and last > first:
        return stripped[first : last + 1]
    return stripped

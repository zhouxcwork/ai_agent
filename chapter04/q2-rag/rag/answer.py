"""证据来源的显示格式。"""

def label(hit_or_node) -> str:
    node = getattr(hit_or_node, "node", hit_or_node)
    head = node.metadata.get("heading") or "(正文)"
    return f"{node.metadata.get('file_path', '?')} § {head}"

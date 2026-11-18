"""核对旧登录时长分块退出新版的两路索引。"""

import argparse
import re
from pathlib import Path

from query import load_bundle

OLD_TTL = re.compile(r"30\s*分钟|30\s*\*\s*60")


def verify(old: Path, new: Path, embed_model=None):
    _, old_nodes, old_manifest = load_bundle(old, embed_model)
    _, new_nodes, new_manifest = load_bundle(new, embed_model)
    stale = {node.node_id for node in old_nodes if OLD_TTL.search(node.text)}
    active = {node.node_id for node in new_nodes}
    if not stale or stale & active or any(OLD_TTL.search(node.text) for node in new_nodes):
        raise ValueError("无法证明旧登录时长分块已退出新版索引")
    print(
        f"版本 {old_manifest['version'][:12]} → {new_manifest['version'][:12]}。"
        f"两路分别核对节点 ID 和正文，{len(stale)} 个旧时长分块均已退出新版。"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old", type=Path)
    parser.add_argument("new", type=Path)
    options = parser.parse_args()
    verify(options.old, options.new)

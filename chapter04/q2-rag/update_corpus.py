"""复制初版资料并将现行登录会话有效期从 30 分钟改为 20 分钟。"""

import argparse
import shutil
from pathlib import Path

CHANGES = {
    "app/session.py": ("30 * 60", "20 * 60", 1),
    "docs/faq.md": ("30 分钟", "20 分钟", 1),
    "docs/payment-session.md": ("登录会话 30 分钟", "登录会话 20 分钟", 1),
    "docs/session-rules.md": ("30 分钟", "20 分钟", 3),
    "docs/testing-guide.md": ("30 分钟", "20 分钟", 1),
    "tables/session-matrix.csv": ("登录会话,30 分钟", "登录会话,20 分钟", 1),
}


def update(source: Path, target: Path):
    updated = {}
    for path, (old, new, count) in CHANGES.items():
        content = (source / path).read_text(encoding="utf-8")
        if content.count(old) != count:
            raise ValueError(f"{path} 不符合初版资料，请从交付语料创建副本")
        updated[path] = content.replace(old, new)
    shutil.copytree(source, target)
    for path, content in updated.items():
        (target / path).write_text(content, encoding="utf-8")
        print(f"更新 {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    update(args.source, args.target)

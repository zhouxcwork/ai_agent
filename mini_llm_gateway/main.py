"""启动器（等价 Java 的 main 类）：PyCharm 里直接 Run / Debug 本文件即可。

- 断点可直接命中：不走 --reload（reload 会 fork 子进程，调试器断点失效）
- 自动加载同目录 .env（setdefault，不覆盖已有环境变量），与 uvicorn --env-file 等价
- 想要热重载的日常开发仍用命令行：uv run --env-file .env uvicorn ... --reload
"""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parent / ".env")
    import uvicorn

    uvicorn.run("mini_llm_gateway.app:create_app", factory=True, host="127.0.0.1", port=8000)

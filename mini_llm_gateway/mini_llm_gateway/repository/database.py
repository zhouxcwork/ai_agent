from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite


@asynccontextmanager
async def connect(database_path: str) -> AsyncIterator[aiosqlite.Connection]:
    # SQLite 连接按操作短生命周期创建，避免跨协程共享连接；确保父目录存在。
    path = Path(database_path)
    if path.parent != Path(""):
        path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(database_path) as db:
        db.row_factory = aiosqlite.Row
        yield db

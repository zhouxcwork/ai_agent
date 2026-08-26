from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from mini_llm_gateway.repository.database import connect
from mini_llm_gateway.schemas.llm import Message


@dataclass
class StoredResponse:
    # 会话续接的存储单元：保存展开后的完整输入（含 instructions/模板 system）与输出文本，
    # 续接 O(1) 重建上下文，无需链式回溯。
    id: str
    requested_model: str
    input_messages: list[Message]
    output_text: str
    input_tokens: int
    output_tokens: int
    created_at: str


class ResponseRepository:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path

    async def initialize(self) -> None:
        async with connect(self.database_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS stored_responses (
                    id TEXT PRIMARY KEY,
                    requested_model TEXT NOT NULL,
                    input_messages_json TEXT NOT NULL,
                    output_text TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.commit()

    async def store(self, response: StoredResponse) -> None:
        async with connect(self.database_path) as db:
            await db.execute(
                "INSERT INTO stored_responses VALUES (?,?,?,?,?,?,?)",
                (
                    response.id,
                    response.requested_model,
                    json.dumps([m.model_dump() for m in response.input_messages], ensure_ascii=False),
                    response.output_text,
                    response.input_tokens,
                    response.output_tokens,
                    response.created_at,
                ),
            )
            await db.commit()

    async def get(self, response_id: str) -> StoredResponse | None:
        async with connect(self.database_path) as db:
            cursor = await db.execute(
                "SELECT id, requested_model, input_messages_json, output_text, "
                "input_tokens, output_tokens, created_at FROM stored_responses WHERE id = ?",
                (response_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        messages = [Message.model_validate(m) for m in json.loads(row[2])]
        return StoredResponse(
            id=row[0],
            requested_model=row[1],
            input_messages=messages,
            output_text=row[3],
            input_tokens=row[4],
            output_tokens=row[5],
            created_at=row[6],
        )

    @staticmethod
    def new_id(request_id: str) -> str:
        return f"resp-{request_id}"

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

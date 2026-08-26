from __future__ import annotations

from mini_llm_gateway.repository.database import connect
from mini_llm_gateway.schemas.trace import CallTrace

_COLUMNS = (
    "request_id, timestamp, requested_model, actual_model, prompt_name, prompt_version, "
    "input_tokens, output_tokens, cost_usd, latency_ms, attempts, status, error_code"
)


class TraceRepository:
    # 调用 trace 的持久化：只追加、不修改，供成本分析与故障排查。
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path

    async def initialize(self) -> None:
        async with connect(self.database_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_call_traces (
                    request_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    requested_model TEXT NOT NULL,
                    actual_model TEXT,
                    prompt_name TEXT,
                    prompt_version TEXT,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    attempts INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_traces_timestamp ON llm_call_traces(timestamp DESC)"
            )
            await db.commit()

    async def insert(self, trace: CallTrace) -> None:
        async with connect(self.database_path) as db:
            await db.execute(
                f"INSERT INTO llm_call_traces ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trace.request_id,
                    trace.timestamp.isoformat(),
                    trace.requested_model,
                    trace.actual_model,
                    trace.prompt_name,
                    trace.prompt_version,
                    trace.input_tokens,
                    trace.output_tokens,
                    trace.cost_usd,
                    trace.latency_ms,
                    trace.attempts,
                    trace.status,
                    trace.error_code,
                ),
            )
            await db.commit()

    async def list(self, limit: int = 50, offset: int = 0) -> list[CallTrace]:
        async with connect(self.database_path) as db:
            cursor = await db.execute(
                f"SELECT {_COLUMNS} FROM llm_call_traces ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            rows = await cursor.fetchall()
        return [CallTrace.model_validate(dict(row)) for row in rows]

from __future__ import annotations

from mini_llm_gateway.repository.database import connect
from mini_llm_gateway.schemas.trace import CallTrace

_COLUMNS = (
    "request_id, timestamp, endpoint, run_id, requested_model, actual_model, prompt_name, prompt_version, "
    "input_tokens, output_tokens, cached_tokens, reasoning_tokens, cost_usd, latency_ms, ttft_ms, "
    "finish_reason, upstream_request_id, attempts, status, error_code"
)

# 旧库迁移（ADR 0012 新增列）：CREATE TABLE IF NOT EXISTS 不会补列，按需 ALTER
_MIGRATION_COLUMNS = {
    "run_id": "TEXT",
    "cached_tokens": "INTEGER NOT NULL DEFAULT 0",
    "reasoning_tokens": "INTEGER NOT NULL DEFAULT 0",
    "ttft_ms": "INTEGER",
    "finish_reason": "TEXT",
    "upstream_request_id": "TEXT",
}


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
                    endpoint TEXT NOT NULL DEFAULT 'llm',
                    run_id TEXT,
                    requested_model TEXT NOT NULL,
                    actual_model TEXT,
                    prompt_name TEXT,
                    prompt_version TEXT,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    ttft_ms INTEGER,
                    finish_reason TEXT,
                    upstream_request_id TEXT,
                    attempts INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT
                )
                """
            )
            cursor = await db.execute("PRAGMA table_info(llm_call_traces)")
            existing = {row[1] for row in await cursor.fetchall()}
            for column, column_type in _MIGRATION_COLUMNS.items():
                if column not in existing:
                    await db.execute(f"ALTER TABLE llm_call_traces ADD COLUMN {column} {column_type}")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_traces_timestamp ON llm_call_traces(timestamp DESC)"
            )
            await db.commit()

    async def insert(self, trace: CallTrace) -> None:
        async with connect(self.database_path) as db:
            await db.execute(
                f"INSERT INTO llm_call_traces ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trace.request_id,
                    trace.timestamp.isoformat(),
                    trace.endpoint,
                    trace.run_id,
                    trace.requested_model,
                    trace.actual_model,
                    trace.prompt_name,
                    trace.prompt_version,
                    trace.input_tokens,
                    trace.output_tokens,
                    trace.cached_tokens,
                    trace.reasoning_tokens,
                    trace.cost_usd,
                    trace.latency_ms,
                    trace.ttft_ms,
                    trace.finish_reason,
                    trace.upstream_request_id,
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

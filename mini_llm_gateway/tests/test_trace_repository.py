from __future__ import annotations

from datetime import datetime, timezone

from mini_llm_gateway.repository.trace_repository import TraceRepository
from mini_llm_gateway.schemas.trace import CallTrace


def make_trace(request_id: str, latency_ms: int) -> CallTrace:
    return CallTrace(
        request_id=request_id,
        timestamp=datetime.now(timezone.utc),
        requested_model="primary",
        actual_model="primary",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.00003,
        latency_ms=latency_ms,
        attempts=1,
        status="success",
    )


async def test_trace_roundtrip(tmp_path):
    repo = TraceRepository(str(tmp_path / "traces.db"))
    await repo.initialize()
    await repo.insert(make_trace("r1", 100))
    await repo.insert(make_trace("r2", 200))

    traces = await repo.list(limit=10, offset=0)
    assert [t.request_id for t in traces] == ["r2", "r1"]  # 按时间倒序
    assert traces[0].input_tokens == 10
    assert traces[0].timestamp.tzinfo is not None

    page = await repo.list(limit=1, offset=1)
    assert [t.request_id for t in page] == ["r1"]

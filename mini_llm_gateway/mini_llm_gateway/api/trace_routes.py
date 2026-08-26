from __future__ import annotations

from fastapi import APIRouter, Query, Request

from mini_llm_gateway.schemas.trace import CallTrace

router = APIRouter()


@router.get("/v1/traces", response_model=list[CallTrace])
async def list_traces(
    http: Request,
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[CallTrace]:
    # 暴露调用审计记录（SQLite 持久化），供成本分析与故障排查使用。
    return await http.app.state.traces.list(limit=limit, offset=offset)

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from mini_llm_gateway.errors import GatewayError
from mini_llm_gateway.schemas.llm import LLMRequest, LLMResponse

router = APIRouter()


@router.post("/v1/llm", response_model=LLMResponse)
async def create_llm_response(request: LLMRequest, http: Request) -> LLMResponse:
    # FastAPI 在入口校验请求、在 response_model 校验统一响应出口。
    if request.stream:
        raise GatewayError("use_stream_endpoint", "流式请求请使用 /v1/llm/stream", 400)
    return await http.app.state.gateway.call(request)


@router.post("/v1/llm/stream")
async def create_stream(request: LLMRequest, http: Request) -> StreamingResponse:
    # 提供独立流式入口，明确禁止与 Structured Output 混用。
    if request.response_schema is not None:
        raise GatewayError("unsupported_combination", "流式输出不支持 response_schema", 400)
    gateway = http.app.state.gateway
    # 返回前先完成模型白名单与模板校验，让错误以 HTTP 状态码暴露而不是 SSE 事件。
    gateway.validate_model(request.model, None)
    await gateway.build_messages(request)
    return StreamingResponse(
        gateway.stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

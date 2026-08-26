from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from mini_llm_gateway.errors import GatewayError
from mini_llm_gateway.schemas.llm import LLMRequest
from mini_llm_gateway.schemas.openai_compat import (
    ChatCompletionRequest,
    normalize_chat_request,
    to_chat_completion,
)

router = APIRouter()

ENDPOINT = "chat_completions"


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(payload: ChatCompletionRequest, http: Request) -> JSONResponse | StreamingResponse:
    # OpenAI Chat Completions 方言入口：请求规范化 → 内部领域模型 → 响应翻译。
    request: LLMRequest = normalize_chat_request(payload)
    gateway = http.app.state.gateway
    if payload.stream:
        await gateway.validate_request(request)
        return StreamingResponse(
            _chunk_stream(gateway, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    response = await gateway.call(request, endpoint=ENDPOINT)
    body = to_chat_completion(
        request_id=response.request_id,
        requested_model=request.model,
        actual_model=response.model,
        content=response.content,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
    return JSONResponse(body.model_dump(), headers={"X-Request-ID": response.request_id})


async def _chunk_stream(gateway: Any, request: LLMRequest) -> AsyncIterator[str]:
    # 领域事件 → OpenAI chat.completion.chunk SSE；首块带 role，尾块 finish_reason，
    # 失败以流内 error 事件收尾（此时不产生半截正文块的 finish_reason）。
    created = int(time.time())
    first = True
    async for event in gateway.stream(request, endpoint=ENDPOINT):
        chunk: dict[str, Any] = {
            "id": f"chatcmpl-{event.get('request_id', '')}",
            "object": "chat.completion.chunk",
            "created": created,
            "model": request.model,
            "actual_model": event.get("model"),
        }
        if event["type"] == "content.delta":
            delta: dict[str, Any] = {"content": event["delta"]}
            if first:
                delta = {"role": "assistant", "content": event["delta"]}
                first = False
            yield _sse({**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
        elif event["type"] == "response.completed":
            yield _sse({**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            # OpenAI 惯例：include_usage 时在 [DONE] 前附加一个 choices 为空的 usage chunk
            if event.get("usage") is not None:
                usage_chunk = {
                    "id": chunk["id"],
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": request.model,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": event["usage"]["input_tokens"],
                        "completion_tokens": event["usage"]["output_tokens"],
                        "total_tokens": event["usage"]["input_tokens"] + event["usage"]["output_tokens"],
                    },
                }
                yield _sse(usage_chunk)
            yield "data: [DONE]\n\n"
        elif event["type"] == "response.failed":
            # OpenAI 惯例：中途错误只发 error 事件，不追加 [DONE]
            yield _sse(
                {
                    "error": {
                        "message": "上游流式调用失败",
                        "type": "gateway_error",
                        "param": None,
                        "code": event.get("error", "upstream_stream_failed"),
                    }
                }
            )

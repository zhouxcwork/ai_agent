from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from mini_llm_gateway.errors import GatewayError
from mini_llm_gateway.repository.response_repository import ResponseRepository, StoredResponse
from mini_llm_gateway.schemas.llm import LLMRequest, Message
from mini_llm_gateway.schemas.responses_compat import (
    ResponsesRequest,
    normalize_responses_request,
    to_response_object,
)

router = APIRouter()

ENDPOINT = "responses"


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/v1/responses", response_model=None)
async def create_response(payload: ResponsesRequest, http: Request) -> JSONResponse | StreamingResponse:
    # OpenAI Responses 方言入口：请求规范化 → 会话上下文展开 → 内部调用 → 响应翻译与存储。
    request: LLMRequest = normalize_responses_request(payload)
    gateway = http.app.state.gateway
    repo: ResponseRepository = http.app.state.responses

    if payload.previous_response_id is not None:
        parent = await repo.get(payload.previous_response_id)
        if parent is None:
            raise GatewayError(
                "response_not_found",
                f"previous_response_id 不存在或未存储: {payload.previous_response_id}",
                404,
            )
        request = _prepend_context(request, parent)

    if payload.stream:
        await gateway.validate_request(request)
        return StreamingResponse(
            _event_stream(gateway, repo, request, store=payload.store),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    full_messages = await gateway.build_messages(request)  # 展开后的完整输入（含模板 system）
    response = await gateway.call(request, endpoint=ENDPOINT)
    response_id = ResponseRepository.new_id(response.request_id)
    if payload.store:
        await repo.store(
            StoredResponse(
                id=response_id,
                requested_model=request.model,
                input_messages=full_messages,
                output_text=response.content,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                created_at=ResponseRepository.now_iso(),
            )
        )
    body = to_response_object(
        request_id=response.request_id,
        requested_model=request.model,
        actual_model=response.model,
        content=response.content,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
    return JSONResponse(body.model_dump(), headers={"X-Request-ID": response.request_id})


async def _event_stream(
    gateway: Any, repo: ResponseRepository, request: LLMRequest, *, store: bool
) -> AsyncIterator[str]:
    # 领域事件 → Responses 类型化事件（最小子集）：created / output_text.delta / completed / failed。
    # 无 [DONE]（Responses 协议无此标记）；completed 携带完整 response 对象。
    created_at = int(time.time())
    text_parts: list[str] = []
    stream_state: dict[str, Any] = {"request_id": "", "model": None, "usage": None}

    def response_skeleton(status: str, output_text: str = "", error: dict[str, Any] | None = None) -> dict[str, Any]:
        usage = stream_state.get("usage") or {"input_tokens": 0, "output_tokens": 0}
        total = usage["input_tokens"] + usage["output_tokens"]
        body: dict[str, Any] = {
            "id": f"resp-{stream_state['request_id']}",
            "object": "response",
            "created_at": created_at,
            "model": request.model,
            "actual_model": stream_state["model"],
            "status": status,
            "output": []
            if not output_text
            else [
                {
                    "type": "message",
                    "id": f"msg-{stream_state['request_id']}",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": output_text, "annotations": []}],
                }
            ],
            "usage": {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"], "total_tokens": total},
        }
        if error is not None:
            body["error"] = error
        return body

    full_messages = await gateway.build_messages(request)
    async for event in gateway.stream(request, endpoint=ENDPOINT):
        stream_state["request_id"] = event.get("request_id", stream_state["request_id"])
        if event["type"] == "content.delta":
            stream_state["model"] = event.get("model", stream_state["model"])
            if not text_parts:
                yield _sse({"type": "response.created", "response": response_skeleton("in_progress")})
            text_parts.append(event["delta"])
            yield _sse(
                {
                    "type": "response.output_text.delta",
                    "item_id": f"msg-{stream_state['request_id']}",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": event["delta"],
                }
            )
        elif event["type"] == "response.completed":
            if not text_parts:
                yield _sse({"type": "response.created", "response": response_skeleton("in_progress")})
            output_text = "".join(text_parts)
            stream_state["model"] = event.get("model", stream_state["model"])
            stream_state["usage"] = event.get("usage")
            if store:
                usage = event.get("usage") or {"input_tokens": 0, "output_tokens": 0}
                await repo.store(
                    StoredResponse(
                        id=f"resp-{stream_state['request_id']}",
                        requested_model=request.model,
                        input_messages=full_messages,
                        output_text=output_text,
                        input_tokens=usage["input_tokens"],
                        output_tokens=usage["output_tokens"],
                        created_at=ResponseRepository.now_iso(),
                    )
                )
            yield _sse({"type": "response.completed", "response": response_skeleton("completed", output_text)})
        elif event["type"] == "response.failed":
            if not text_parts:
                yield _sse({"type": "response.created", "response": response_skeleton("in_progress")})
            yield _sse(
                {
                    "type": "response.failed",
                    "response": response_skeleton(
                        "failed",
                        error={
                            "message": "上游流式调用失败",
                            "type": "gateway_error",
                            "code": event.get("error", "upstream_stream_failed"),
                        },
                    ),
                }
            )


def _prepend_context(request: LLMRequest, parent: StoredResponse) -> LLMRequest:
    # 本轮 instructions（若有）保持最前，父响应完整输入 + 其输出紧随，新输入最后；
    # 父记录存的就是展开后的完整输入，O(1) 重建上下文。
    context = [*parent.input_messages, Message(role="assistant", content=parent.output_text)]
    instructions_at_front = 1 if request.messages and request.messages[0].role == "system" else 0
    messages = [*request.messages[:instructions_at_front], *context, *request.messages[instructions_at_front:]]
    return request.model_copy(update={"messages": messages})

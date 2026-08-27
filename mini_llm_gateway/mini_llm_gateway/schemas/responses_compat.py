from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mini_llm_gateway.errors import GatewayError
from mini_llm_gateway.schemas.llm import LLMRequest, Message, PromptSelection

# ---- 入站：OpenAI Responses 方言 ----
# 未知字段静默忽略；采样参数接受但忽略（与 chat 端点同一策略）。


class ResponseContentBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    text: str | None = None


class ResponseMessageIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[ResponseContentBlock]


class TextFormat(BaseModel):
    model_config = ConfigDict(extra="allow")

    format: dict[str, Any] | None = None


class ResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    input: str | list[ResponseMessageIn]
    instructions: str | None = None
    stream: bool = False
    previous_response_id: str | None = None  # 会话续接：引用已存储的父响应
    store: bool = True  # false 则不入库，不可被续接
    text: TextFormat | None = None
    gateway_prompt: PromptSelection | None = None
    # 采样参数接受但忽略
    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    reasoning: dict[str, Any] | None = None
    truncation: str | None = None
    user: str | None = None


# ---- 出站：OpenAI Responses 方言 ----


class OutputTextContent(BaseModel):
    type: str = "output_text"
    text: str
    annotations: list[Any] = Field(default_factory=list)


class OutputMessage(BaseModel):
    type: str = "message"
    id: str
    role: str = "assistant"
    status: str = "completed"
    content: list[OutputTextContent]


class ResponseUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int


class ResponseObject(BaseModel):
    id: str
    object: str = "response"
    created_at: int
    model: str  # requested_model（档位名）；路由目标不对外透出，仅记入 Trace（ADR 0015）
    status: str = "completed"
    incomplete_details: dict[str, str] | None = None  # status=incomplete 时的原因（如 content_filter）
    output: list[OutputMessage]
    usage: ResponseUsage


# ---- 请求规范化：Responses 方言 → 内部领域模型 ----

_ROLE_MAP = {"system": "system", "developer": "system", "user": "user", "assistant": "assistant"}
_TEXT_BLOCK_TYPES = {"input_text", "output_text", "text", "summary_text"}
_UNSUPPORTED_PARAMS = ("tools", "tool_choice", "parallel_tool_calls")


def normalize_responses_request(payload: ResponsesRequest) -> LLMRequest:
    # Responses 请求 → 内部 LLMRequest；input 三形态、instructions、结构化在此归一。
    for param in _UNSUPPORTED_PARAMS:
        if getattr(payload, param, None) is not None:
            raise GatewayError("request.unsupported_parameter", f"网关不支持参数: {param}", 400)

    messages: list[Message] = []
    if payload.instructions is not None:
        messages.append(Message(role="system", content=payload.instructions))

    if isinstance(payload.input, str):
        messages.append(Message(role="user", content=payload.input))
    else:
        for item in payload.input:
            role = _ROLE_MAP.get(item.role)
            if role is None:
                raise GatewayError("request.unsupported_parameter", f"不支持的消息角色: {item.role}", 400)
            messages.append(Message(role=role, content=_flatten_content(item.content)))

    response_schema = None
    text_format = (payload.text.format if payload.text else None) or {}
    format_type = text_format.get("type")
    if format_type == "json_schema":
        schema = text_format.get("schema")
        if not isinstance(schema, dict):
            raise GatewayError("request.unsupported_parameter", "text.format.schema 缺失", 400)
        response_schema = schema
    elif format_type == "json_object":
        response_schema = {"type": "object"}
    # 流式与结构化已解禁（ADR 0008）：流中语法监控、流尾 Schema 裁决

    return LLMRequest(
        model=payload.model,
        messages=messages,
        stream=payload.stream,
        response_schema=response_schema,
        prompt=payload.gateway_prompt,
    )


def _flatten_content(content: str | list[ResponseContentBlock]) -> str:
    if isinstance(content, str):
        return content
    texts = []
    for block in content:
        if block.type not in _TEXT_BLOCK_TYPES or block.text is None:
            raise GatewayError("request.unsupported_parameter", f"不支持的内容块类型: {block.type}", 400)
        texts.append(block.text)
    joined = "\n".join(texts)
    if not joined:
        raise GatewayError("request.unsupported_parameter", "消息内容不能为空", 400)
    return joined


def to_response_object(
    request_id: str, requested_model: str, content: str,
    input_tokens: int, output_tokens: int, finish_reason: str = "stop",
) -> ResponseObject:
    # 内部结果 → OpenAI response 对象；usage 用 responses 协议字段名。
    # content_filter（安全拒答）按 Responses 方言表达：status=incomplete + incomplete_details（ADR 0010）。
    refused = finish_reason == "content_filter"
    return ResponseObject(
        id=f"resp-{request_id}",
        created_at=int(time.time()),
        model=requested_model,
        status="incomplete" if refused else "completed",
        incomplete_details={"reason": "content_filter"} if refused else None,
        output=[OutputMessage(id=f"msg-{request_id}", content=[OutputTextContent(text=content)])],
        usage=ResponseUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
    )

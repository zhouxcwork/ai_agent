from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mini_llm_gateway.errors import GatewayError
from mini_llm_gateway.schemas.llm import LLMRequest, Message, PromptSelection

# ---- 入站：OpenAI Chat Completions 方言 ----
# 未知字段静默忽略（extra=allow，对齐 OpenAI 宽松行为）；
# 采样参数声明出来仅为协议兼容，网关不透传。


class ChatMessageIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str = Field(min_length=1)


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessageIn] = Field(min_length=1)
    stream: bool = False
    response_format: dict[str, Any] | None = None
    gateway_prompt: PromptSelection | None = None  # 非标扩展：模板选择，SDK 经 extra_body 传入
    # 以下采样参数接受但忽略
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    seed: int | None = None
    stop: str | list[str] | None = None
    user: str | None = None


# ---- 出站：OpenAI Chat Completions 方言 ----


class ChatMessageOut(BaseModel):
    role: str = "assistant"
    content: str


class ChatChoice(BaseModel):
    index: int = 0
    message: ChatMessageOut
    finish_reason: str = "stop"


class ChatUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str  # requested_model（档位名，见 ADR 0004）
    actual_model: str  # 扩展字段：实际命中的路由目标 供应商/模型
    choices: list[ChatChoice]
    usage: ChatUsage


# ---- 请求规范化：外部方言 → 内部领域模型（协议分离边界） ----

_ROLE_MAP = {"system": "system", "developer": "system", "user": "user", "assistant": "assistant"}
_UNSUPPORTED_PARAMS = ("tools", "tool_choice", "n", "logprobs", "parallel_tool_calls")


def normalize_chat_request(payload: ChatCompletionRequest) -> LLMRequest:
    # OpenAI Chat Completions 请求 → 内部 LLMRequest；不支持的组合与能力在此拒绝。
    for param in _UNSUPPORTED_PARAMS:
        if getattr(payload, param, None) not in (None, 1):
            raise GatewayError("request.unsupported_parameter", f"网关不支持参数: {param}", 400)

    messages = []
    for message in payload.messages:
        role = _ROLE_MAP.get(message.role)
        if role is None:
            raise GatewayError("request.unsupported_parameter", f"不支持的消息角色: {message.role}", 400)
        messages.append(Message(role=role, content=message.content))

    response_schema = _extract_response_schema(payload.response_format)
    # 流式与结构化已解禁（ADR 0008）：流中语法监控、流尾 Schema 裁决

    return LLMRequest(
        model=payload.model,
        messages=messages,
        stream=payload.stream,
        response_schema=response_schema,
        prompt=payload.gateway_prompt,
    )


def _extract_response_schema(response_format: dict[str, Any] | None) -> dict[str, Any] | None:
    if response_format is None:
        return None
    format_type = response_format.get("type")
    if format_type == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema")
        if not isinstance(schema, dict):
            raise GatewayError("request.unsupported_parameter", "response_format.json_schema.schema 缺失", 400)
        return schema
    if format_type == "json_object":
        return {"type": "object"}
    return None  # text 等价于默认自由文本


def to_chat_completion(request_id: str, requested_model: str, actual_model: str, content: str,
                       input_tokens: int, output_tokens: int, finish_reason: str = "stop") -> ChatCompletionResponse:
    # 内部结果 → OpenAI chat.completion 响应；model 回显档位名，actual_model 透出路由目标。
    # content_filter（安全拒答）按 OpenAI 方言透传：200 + finish_reason（ADR 0010）。
    return ChatCompletionResponse(
        id=f"chatcmpl-{request_id}",
        created=int(time.time()),
        model=requested_model,
        actual_model=actual_model,
        choices=[ChatChoice(message=ChatMessageOut(content=content), finish_reason=finish_reason)],
        usage=ChatUsage(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
    )

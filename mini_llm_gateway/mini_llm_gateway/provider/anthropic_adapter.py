from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from anthropic import AsyncAnthropic

from mini_llm_gateway.config import ResolvedTarget
from mini_llm_gateway.errors import GatewayError
from mini_llm_gateway.provider.base import (
    ContentRefusedError,
    FinishReason,
    RetryableUpstreamError,
    UpstreamID,
    UpstreamRejectedError,
)
from mini_llm_gateway.schemas.llm import Message, Usage

# Anthropic Messages 协议要求 max_tokens 必填；内部请求未建模输出上限，统一取宽松默认值
_MAX_TOKENS = 8192

_TOOL_NAME = "emit_json"

_ANTHROPIC_STOP_REASON = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "refusal": "content_filter",
}


def _map_stop_reason(stop_reason: str | None) -> str:
    return _ANTHROPIC_STOP_REASON.get(stop_reason or "", "stop")


def _translate_anthropic_error(exc: Exception) -> Exception:
    # anthropic SDK 异常在适配层翻译为内部异常（口径与 openai 适配器一致，ADR 0013）：
    # 瞬态（连接/超时/429/5xx）→ 可重试；明确拒绝（401/403/404）→ 换候选不重试。
    from anthropic import APIConnectionError, APIStatusError, APITimeoutError

    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return RetryableUpstreamError(str(exc))
    if isinstance(exc, APIStatusError):
        if exc.status_code == 429 or exc.status_code >= 500:
            return RetryableUpstreamError(str(exc))
        if exc.status_code in (401, 403, 404):
            return UpstreamRejectedError(str(exc))
    return exc


def _translate_messages(messages: list[Message]) -> tuple[str | None, list[dict[str, str]]]:
    # Anthropic 协议 system 是顶层参数而非消息；其余角色原样进 messages。
    system_parts = [m.content for m in messages if m.role == "system"]
    conversation = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
    return ("\n\n".join(system_parts) or None), conversation


def _usage_from_anthropic(usage: Any) -> Usage:
    # 防御性提取（ADR 0012）：Anthropic 的 input_tokens 不含缓存部分（cache_read/cache_creation
    # 是独立字段），并入总量以对齐领域口径"cached ⊆ input"；cached 只记读命中（计价走缓存价）。
    # reasoning 细分 Anthropic 不提供，记 0。
    plain_input = getattr(usage, "input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", None) or 0
    return Usage(
        input_tokens=plain_input + cache_read + cache_creation,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cached_tokens=cache_read,
        reasoning_tokens=0,
    )


class AnthropicAdapter:
    # Anthropic Messages 协议适配器（ADR 0013）：鉴权用 x-api-key、结构化走 tool-use 强制、
    # 流事件（message_start/content_block_delta/message_delta）翻译为统一信号流。
    def create_client(self, target: ResolvedTarget) -> AsyncAnthropic:
        api_key = os.getenv(target.api_key_env)
        if not api_key:
            raise GatewayError("platform.misconfigured", "Gateway 模型凭据未配置", 503)
        return AsyncAnthropic(api_key=api_key, base_url=target.base_url, max_retries=0)

    @staticmethod
    def _build_request(
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None,
    ) -> dict[str, Any]:
        system, conversation = _translate_messages(messages)
        request: dict[str, Any] = {
            "model": target.provider_model,
            "max_tokens": _MAX_TOKENS,
            "messages": conversation,
            "timeout": timeout_seconds,
        }
        if system is not None:
            request["system"] = system
        if response_schema is not None:
            # Anthropic 协议没有 response_format：注册输出工具并强制调用，
            # 从 tool_use 内容块提取 JSON（第一层保证，ADR 0013）。
            # DeepSeek V4 是 thinking 模型且 thinking 与 tool_choice 强制互斥，结构化场景显式关闭。
            request["thinking"] = {"type": "disabled"}
            request["tools"] = [
                {
                    "name": _TOOL_NAME,
                    "description": "以结构化 JSON 输出结果",
                    "input_schema": response_schema,
                }
            ]
            request["tool_choice"] = {"type": "tool", "name": _TOOL_NAME}
        return request

    async def complete(
        self,
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None,
    ) -> tuple[str, Usage, str | None]:
        request = self._build_request(target, messages, timeout_seconds, response_schema)
        try:
            response = await self.create_client(target).messages.create(**request)
        except Exception as exc:
            raise _translate_anthropic_error(exc) from exc
        if response.stop_reason == "refusal":
            raise ContentRefusedError(f"上游内容策略拒答: {target.key}")
        content = self._extract_content(response.content)
        return content, _usage_from_anthropic(response.usage), response.id

    @staticmethod
    def _extract_content(blocks: list[Any]) -> str:
        # 结构化时 tool_use.input 即结果（序列化回字符串，两层校验照常工作）；否则拼接文本块。
        for block in blocks:
            if getattr(block, "type", None) == "tool_use":
                return json.dumps(block.input, ensure_ascii=False)
        return "".join(getattr(block, "text", "") for block in blocks if getattr(block, "type", None) == "text")

    async def stream(
        self,
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None = None,
    ) -> AsyncIterator[str | Usage | FinishReason | UpstreamID]:
        request = self._build_request(target, messages, timeout_seconds, response_schema)
        request["stream"] = True
        plain_input = 0
        cache_read = 0
        cache_creation = 0
        try:
            events = await self.create_client(target).messages.create(**request)
            async for event in events:
                if event.type == "message_start":
                    message = event.message
                    if getattr(message, "id", None):
                        yield UpstreamID(message.id)
                    # 口径同 _usage_from_anthropic：缓存字段独立，input 需并入总量
                    plain_input = getattr(message.usage, "input_tokens", 0) or 0
                    cache_read = getattr(message.usage, "cache_read_input_tokens", None) or 0
                    cache_creation = getattr(message.usage, "cache_creation_input_tokens", None) or 0
                elif event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield delta.text
                    elif delta.type == "input_json_delta":
                        # 结构化流式：tool 参数增量即业务增量，供语法监控与流尾拼接
                        yield delta.partial_json
                elif event.type == "message_delta":
                    stop_reason = _map_stop_reason(event.delta.stop_reason)
                    usage = Usage(
                        input_tokens=plain_input + cache_read + cache_creation,
                        output_tokens=getattr(event.usage, "output_tokens", 0) or 0,
                        cached_tokens=cache_read,
                        reasoning_tokens=0,
                    )
                    yield FinishReason(stop_reason)
                    yield usage
        except Exception as exc:
            raise _translate_anthropic_error(exc) from exc

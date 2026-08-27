from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from mini_llm_gateway.config import ResolvedTarget
from mini_llm_gateway.errors import GatewayError
from mini_llm_gateway.provider.base import ContentRefusedError, FinishReason, UpstreamID
from mini_llm_gateway.provider.openai_compatible import _translate_openai_error
from mini_llm_gateway.schemas.llm import Message, Usage


def _usage_from_responses(usage: Any) -> Usage:
    # 防御性提取（ADR 0012）：input_tokens_details.cached / output_tokens_details.reasoning 缺失记 0。
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    return Usage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cached_tokens=getattr(input_details, "cached_tokens", None) or 0,
        reasoning_tokens=getattr(output_details, "reasoning_tokens", None) or 0,
    )


class ResponsesAdapter:
    # OpenAI Responses 协议适配器（ADR 0013）：system 合并进 instructions、结构化走 text.format、
    # 流事件（response.created/output_text.delta/completed）翻译为统一信号流。
    # 复用 openai SDK（与 chat completions 同一客户端），异常翻译口径一致。
    def create_client(self, target: ResolvedTarget) -> AsyncOpenAI:
        api_key = os.getenv(target.api_key_env)
        if not api_key:
            raise GatewayError("platform.misconfigured", "Gateway 模型凭据未配置", 503)
        return AsyncOpenAI(api_key=api_key, base_url=target.base_url, max_retries=0)

    @staticmethod
    def _build_request(
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None,
    ) -> dict[str, Any]:
        instructions = "\n\n".join(m.content for m in messages if m.role == "system") or None
        request: dict[str, Any] = {
            "model": target.provider_model,
            "input": [
                {"role": m.role, "content": m.content} for m in messages if m.role != "system"
            ],
            "timeout": timeout_seconds,
        }
        if instructions is not None:
            request["instructions"] = instructions
        if response_schema is not None:
            # Responses 协议的结构化参数是 text.format（第一层保证，ADR 0013）
            if target.structured_output_mode == "json_object":
                request["text"] = {"format": {"type": "json_object"}}
                request["instructions"] = (
                    f"{request.get('instructions') or ''}\n\n只返回一个合法 JSON 对象，"
                    "必须严格符合下列 JSON Schema，不要返回 Markdown 或额外文字："
                    f"{json.dumps(response_schema, ensure_ascii=False)}"
                ).strip()
            else:
                request["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": "agent_response",
                        "strict": True,
                        "schema": response_schema,
                    }
                }
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
            response = await self.create_client(target).responses.create(**request)
        except Exception as exc:
            raise _translate_openai_error(exc) from exc
        refusal = getattr(getattr(response, "text", None), "refusal", None)
        if refusal:
            raise ContentRefusedError(f"上游内容策略拒答: {target.key}")
        return response.output_text, _usage_from_responses(response.usage), response.id

    async def stream(
        self,
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None = None,
    ) -> AsyncIterator[str | Usage | FinishReason | UpstreamID]:
        request = self._build_request(target, messages, timeout_seconds, response_schema)
        request["stream"] = True
        try:
            events = await self.create_client(target).responses.create(**request)
            async for event in events:
                if event.type == "response.created":
                    if getattr(event.response, "id", None):
                        yield UpstreamID(event.response.id)
                elif event.type == "response.output_text.delta":
                    yield event.delta
                elif event.type in ("response.completed", "response.incomplete"):
                    response = event.response
                    stop = "stop"
                    if event.type == "response.incomplete":
                        reason = getattr(response, "incomplete_details", None)
                        stop = "length" if getattr(reason, "reason", None) == "max_output_tokens" else "stop"
                    yield FinishReason(stop)
                    yield _usage_from_responses(getattr(response, "usage", None))
        except Exception as exc:
            raise _translate_openai_error(exc) from exc

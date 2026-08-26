from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

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


def _extract_usage(usage: Any) -> Usage:
    # 防御性提取：prompt/completion tokens 的 details 各供应商支持不一，缺失记 0。
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    return Usage(
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
        cached_tokens=getattr(prompt_details, "cached_tokens", None) or 0,
        reasoning_tokens=getattr(completion_details, "reasoning_tokens", None) or 0,
    )


def _translate_openai_error(exc: Exception) -> Exception:
    # 供应商 SDK 异常在适配层翻译为内部异常，service 层不 import openai。
    # 瞬态（连接/超时/429/5xx）→ 可重试；明确拒绝（401/403/404）→ 换候选不重试；其余原样。
    from openai import APIConnectionError, APIStatusError, APITimeoutError

    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return RetryableUpstreamError(str(exc))
    if isinstance(exc, APIStatusError):
        if exc.status_code == 429 or exc.status_code >= 500:
            return RetryableUpstreamError(str(exc))
        if exc.status_code in (401, 403, 404):
            return UpstreamRejectedError(str(exc))
    return exc


class OpenAICompatibleProvider:
    # 实现 OpenAI Compatible Adapter，集中处理供应商协议与认证细节。
    # 将 API Key 保留在 Gateway 内，业务 Agent 无需接触供应商密钥。
    def create_client(self, target: ResolvedTarget) -> AsyncOpenAI:
        api_key = os.getenv(target.api_key_env)
        if not api_key:
            raise GatewayError("platform.misconfigured", "Gateway 模型凭据未配置", 503)
        return AsyncOpenAI(api_key=api_key, base_url=target.base_url, max_retries=0)

    @staticmethod
    def _build_request_data(
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None,
    ) -> dict[str, Any]:
        # 统一构造请求体：结构化第一层保证（供应商模式或 schema 注入），流式/非流式共用。
        request_data: dict[str, Any] = {
            "model": target.provider_model,
            "messages": [message.model_dump() for message in messages],
            "timeout": timeout_seconds,
        }
        if response_schema is not None:
            if target.structured_output_mode == "json_schema":
                request_data["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "agent_response",
                        "strict": True,
                        "schema": response_schema,
                    },
                }
            else:
                request_data["response_format"] = {"type": "json_object"}
                request_data["messages"] = [
                    {
                        "role": "system",
                        "content": (
                            "只返回一个合法 JSON 对象，必须严格符合下列 JSON Schema，"
                            "不要返回 Markdown 或额外文字："
                            f"{json.dumps(response_schema, ensure_ascii=False)}"
                        ),
                    },
                    *request_data["messages"],
                ]
        return request_data

    async def complete(
        self,
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None,
    ) -> tuple[str, Usage, str | None]:
        # 将统一请求转换为 OpenAI Compatible 调用，隔离厂商协议差异。
        request_data = self._build_request_data(target, messages, timeout_seconds, response_schema)
        try:
            completion = await self.create_client(target).chat.completions.create(**request_data)
        except Exception as exc:
            raise _translate_openai_error(exc) from exc
        choice = completion.choices[0]
        if choice.finish_reason == "content_filter":
            raise ContentRefusedError(f"上游内容策略拒答: {target.key}")
        content = choice.message.content or ""
        return content, _extract_usage(completion.usage), completion.id

    async def stream(
        self,
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None = None,
    ) -> AsyncIterator[str | Usage | FinishReason | UpstreamID]:
        # 逐块读取上游响应；结构化流式同样走第一层保证（模式/schema 注入，
        # 见 ADR 0008），并开启 include_usage 使末块携带真实 token 用量。
        request_data = self._build_request_data(target, messages, timeout_seconds, response_schema)
        request_data["stream"] = True
        request_data["stream_options"] = {"include_usage": True}
        try:
            response = await self.create_client(target).chat.completions.create(**request_data)
            async for chunk in response:
                if chunk.id:
                    yield UpstreamID(chunk.id)  # 全流共享同一上游请求 ID，首个信号即够
                choice = chunk.choices[0] if chunk.choices else None
                if choice and choice.finish_reason:
                    yield FinishReason(choice.finish_reason)
                elif choice and choice.delta.content:
                    yield choice.delta.content
                elif chunk.usage is not None:
                    yield _extract_usage(chunk.usage)
        except Exception as exc:
            raise _translate_openai_error(exc) from exc

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from mini_llm_gateway.config import ResolvedTarget
from mini_llm_gateway.errors import GatewayError
from mini_llm_gateway.provider.base import RetryableUpstreamError
from mini_llm_gateway.schemas.llm import Message, Usage


def _translate_openai_error(exc: Exception) -> Exception:
    # 供应商 SDK 异常在适配层翻译为内部异常，service 层不 import openai。
    from openai import APIConnectionError, APITimeoutError, RateLimitError

    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError)):
        return RetryableUpstreamError(str(exc))
    return exc


class OpenAICompatibleProvider:
    # 实现 OpenAI Compatible Adapter，集中处理供应商协议与认证细节。
    # 将 API Key 保留在 Gateway 内，业务 Agent 无需接触供应商密钥。
    def create_client(self, target: ResolvedTarget) -> AsyncOpenAI:
        api_key = os.getenv(target.api_key_env)
        if not api_key:
            raise GatewayError("gateway_misconfigured", "Gateway 模型凭据未配置", 503)
        return AsyncOpenAI(api_key=api_key, base_url=target.base_url, max_retries=0)

    async def complete(
        self,
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None,
    ) -> tuple[str, Usage]:
        # 将统一请求转换为 OpenAI Compatible 调用，隔离厂商协议差异。
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
        try:
            completion = await self.create_client(target).chat.completions.create(**request_data)
        except Exception as exc:
            raise _translate_openai_error(exc) from exc
        content = completion.choices[0].message.content or ""
        usage = completion.usage
        return content, Usage(
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )

    async def stream(
        self,
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
    ) -> AsyncIterator[str]:
        # 逐块读取上游响应，为 Gateway 的实时流式代理提供标准增量文本。
        try:
            response = await self.create_client(target).chat.completions.create(
                model=target.provider_model,
                messages=[message.model_dump() for message in messages],
                stream=True,
                timeout=timeout_seconds,
            )
            async for chunk in response:
                choice = chunk.choices[0] if chunk.choices else None
                if choice and choice.delta.content:
                    yield choice.delta.content
        except Exception as exc:
            raise _translate_openai_error(exc) from exc

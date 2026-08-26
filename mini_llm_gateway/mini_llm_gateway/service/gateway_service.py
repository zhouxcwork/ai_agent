from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Literal

from jsonschema import ValidationError as JsonSchemaError
from jsonschema import validate
from openai import APIConnectionError, APITimeoutError, RateLimitError

from mini_llm_gateway.config import GatewayConfig, ModelConfig
from mini_llm_gateway.errors import GatewayError
from mini_llm_gateway.provider.base import Provider
from mini_llm_gateway.repository.prompt_repository import PromptRepository
from mini_llm_gateway.repository.trace_repository import TraceRepository
from mini_llm_gateway.schemas.llm import LLMRequest, LLMResponse, Message, Usage
from mini_llm_gateway.schemas.trace import CallTrace

logger = logging.getLogger("llm_gateway")


def is_retryable(exc: Exception) -> bool:
    return isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError, TimeoutError, ConnectionError))


class GatewayService:
    # Gateway 业务编排：模板渲染、模型校验、fallback 重试、成本计算、trace 落库。
    def __init__(
        self,
        config: GatewayConfig,
        provider: Provider,
        prompt_repository: PromptRepository,
        trace_repository: TraceRepository,
    ) -> None:
        self.config = config
        self.provider = provider
        self.prompts = prompt_repository
        self.traces = trace_repository

    def validate_model(self, model: str, response_schema: dict[str, Any] | None) -> ModelConfig:
        # 校验模型白名单和结构化能力，阻止不等价的 fallback。
        config = self.config.models.get(model)
        if config is None:
            raise GatewayError("unknown_model", "模型不在 Gateway 允许列表中", 400)
        if response_schema is not None and not config.supports_structured_output:
            raise GatewayError("structured_output_unsupported", "模型不支持 Structured Output", 400)
        return config

    def calculate_cost(self, model: str, usage: Usage) -> float:
        # 按实际模型和输入输出 Token 计算本次调用成本。
        price = self.config.models[model].price_per_million
        return (
            usage.input_tokens * price.get("input", 0.0) + usage.output_tokens * price.get("output", 0.0)
        ) / 1_000_000

    async def record_trace(
        self,
        request_id: str,
        requested_model: str,
        actual_model: str | None,
        prompt_name: str | None,
        prompt_version: str | None,
        usage: Usage,
        latency_ms: int,
        attempts: int,
        status: Literal["success", "failed"],
        error_code: str | None = None,
    ) -> None:
        # 留存调用元数据；trace 写库失败只降级为日志，不影响主请求。
        trace = CallTrace(
            request_id=request_id,
            timestamp=datetime.now(timezone.utc),
            requested_model=requested_model,
            actual_model=actual_model,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=self.calculate_cost(actual_model, usage) if actual_model else 0.0,
            latency_ms=latency_ms,
            attempts=attempts,
            status=status,
            error_code=error_code,
        )
        try:
            await self.traces.insert(trace)
        except Exception:
            logger.exception("trace 写入数据库失败 request_id=%s", request_id)
        logger.info("llm_call_trace=%s", trace.model_dump_json())

    async def build_messages(self, request: LLMRequest) -> list[Message]:
        # 将模板系统消息统一注入调用上下文，避免 Prompt 分散在各个 Agent 中。
        if request.prompt is None:
            return request.messages
        record = await self.prompts.require(request.prompt.name, request.prompt.version)
        rendered = self.prompts.render(record, request.prompt.variables)
        return [rendered, *request.messages]

    async def call(self, request: LLMRequest) -> LLMResponse:
        # 对临时故障有限重试，并在主模型不可用时切换能力等价的备用模型。
        requested_model = request.model
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        attempts = 0
        last_error: Exception | None = None
        messages = await self.build_messages(request)
        for model_name in dict.fromkeys([requested_model, self.config.fallback_model]):
            try:
                config = self.validate_model(model_name, request.response_schema)
            except GatewayError as exc:
                if model_name == requested_model:
                    raise
                last_error = exc
                continue
            for retry_number in range(2):
                attempts += 1
                try:
                    content, usage = await self.provider.complete(
                        config, messages, request.timeout_seconds, request.response_schema
                    )
                    parsed: dict[str, Any] | list[Any] | None = None
                    if request.response_schema is not None:
                        try:
                            parsed = json.loads(content)
                            validate(instance=parsed, schema=request.response_schema)
                        except json.JSONDecodeError as exc:
                            raise GatewayError("invalid_json", "模型没有返回合法 JSON") from exc
                        except JsonSchemaError as exc:
                            raise GatewayError("schema_validation_failed", "模型结果不符合 response_schema") from exc
                    response = LLMResponse(
                        request_id=request_id,
                        model=model_name,
                        content=content,
                        parsed=parsed,
                        usage=usage,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        attempts=attempts,
                    )
                    await self.record_trace(
                        request_id,
                        requested_model,
                        model_name,
                        request.prompt.name if request.prompt else None,
                        request.prompt.version if request.prompt else None,
                        usage,
                        response.latency_ms,
                        attempts,
                        "success",
                    )
                    return response
                except GatewayError:
                    raise
                except Exception as exc:
                    last_error = exc
                    if is_retryable(exc) and retry_number == 0:
                        await asyncio.sleep(0.1)
                        continue
                    break
        latency_ms = int((time.perf_counter() - started) * 1000)
        await self.record_trace(
            request_id,
            requested_model,
            None,
            request.prompt.name if request.prompt else None,
            request.prompt.version if request.prompt else None,
            Usage(input_tokens=0, output_tokens=0),
            latency_ms,
            attempts,
            "failed",
            "model_unavailable",
        )
        raise GatewayError("model_unavailable", "主模型和备用模型均不可用") from last_error

    async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        # 上游首块前可切备用模型；首块后仅发送流内错误，避免文本重复。
        messages = await self.build_messages(request)
        started = time.perf_counter()
        attempts = 0
        emitted = False
        last_error: Exception | None = None
        for model_name in dict.fromkeys([request.model, self.config.fallback_model]):
            try:
                config = self.validate_model(model_name, None)
                attempts += 1
                async for delta in self.provider.stream(config, messages, request.timeout_seconds):
                    emitted = True
                    yield self.encode_sse({"type": "content.delta", "delta": delta})
                await self.record_trace(
                    str(uuid.uuid4()),
                    request.model,
                    model_name,
                    request.prompt.name if request.prompt else None,
                    request.prompt.version if request.prompt else None,
                    Usage(input_tokens=0, output_tokens=0),
                    int((time.perf_counter() - started) * 1000),
                    attempts,
                    "success",
                )
                yield self.encode_sse({"type": "response.completed", "model": model_name})
                return
            except Exception as exc:
                last_error = exc
                if emitted or not is_retryable(exc):
                    break
        logger.exception("upstream stream failed", exc_info=last_error)
        await self.record_trace(
            str(uuid.uuid4()),
            request.model,
            None,
            request.prompt.name if request.prompt else None,
            request.prompt.version if request.prompt else None,
            Usage(input_tokens=0, output_tokens=0),
            int((time.perf_counter() - started) * 1000),
            attempts,
            "failed",
            "upstream_stream_failed",
        )
        yield self.encode_sse({"type": "response.failed", "error": "upstream_stream_failed"})

    @staticmethod
    def encode_sse(event: dict[str, Any]) -> str:
        # 将统一事件编码为浏览器和 Agent 都可消费的 SSE 格式。
        return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

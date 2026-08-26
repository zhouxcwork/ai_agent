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

from mini_llm_gateway.config import GatewayConfig, ResolvedTarget
from mini_llm_gateway.errors import GatewayError
from mini_llm_gateway.provider.base import Provider, RetryableUpstreamError
from mini_llm_gateway.repository.prompt_repository import PromptRepository
from mini_llm_gateway.repository.trace_repository import TraceRepository
from mini_llm_gateway.schemas.llm import LLMRequest, LLMResponse, Message, Usage
from mini_llm_gateway.schemas.trace import CallTrace
from mini_llm_gateway.service.model_router import ModelRouter

logger = logging.getLogger("llm_gateway")

ATTEMPTS_PER_TARGET = 2


def is_retryable(exc: Exception) -> bool:
    # 只认内部统一的瞬态异常（供应商 SDK 异常已在适配层翻译），service 不感知 openai。
    return isinstance(exc, (RetryableUpstreamError, TimeoutError, ConnectionError))


class GatewayService:
    # Gateway 业务编排：模板渲染、档位路由、候选 fallback、成本计算、熔断回报、trace 落库。
    def __init__(
        self,
        config: GatewayConfig,
        router: ModelRouter,
        provider: Provider,
        prompt_repository: PromptRepository,
        trace_repository: TraceRepository,
    ) -> None:
        self.config = config
        self.router = router
        self.provider = provider
        self.prompts = prompt_repository
        self.traces = trace_repository

    def calculate_cost(self, target: ResolvedTarget, usage: Usage) -> float:
        # 按实际命中路由目标的单价计算本次调用成本。
        price = target.price_per_million
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
        cost_usd: float | None = None,
        target: ResolvedTarget | None = None,
        endpoint: str = "chat_completions",
    ) -> None:
        # 留存调用元数据；trace 写库失败只降级为日志，不影响主请求。
        trace = CallTrace(
            request_id=request_id,
            timestamp=datetime.now(timezone.utc),
            endpoint=endpoint,
            requested_model=requested_model,
            actual_model=actual_model,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=cost_usd if cost_usd is not None else (self.calculate_cost(target, usage) if target else 0.0),
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

    def resolve_candidates(self, request: LLMRequest) -> list[ResolvedTarget]:
        # 档位解析 + 健康度过滤 + 结构化能力过滤；无可用目标时抛出网关错误。
        return self.router.candidates(request.model, structured=request.response_schema is not None)

    async def validate_request(self, request: LLMRequest) -> None:
        # 流式入口的预校验：档位/健康候选/模板问题以 HTTP 状态码暴露而非流内事件；
        # 不推进轮询计数器，避免与真正选路重复消耗轮转位置。
        structured = request.response_schema is not None
        if not self.router.has_candidates(request.model, structured=structured):
            raise GatewayError("no_healthy_route", f"档位 {request.model} 没有健康且能力匹配的路由目标", 503)
        await self.build_messages(request)

    async def call(self, request: LLMRequest, endpoint: str = "chat_completions") -> LLMResponse:
        # 逐候选目标尝试：每目标有限重试，失败切换下一健康候选并回报熔断器。
        requested_model = request.model
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        attempts = 0
        last_error: Exception | None = None
        messages = await self.build_messages(request)
        candidates = self.resolve_candidates(request)
        for target in candidates:
            for retry_number in range(ATTEMPTS_PER_TARGET):
                attempts += 1
                try:
                    content, usage = await self.provider.complete(
                        target, messages, request.timeout_seconds, request.response_schema
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
                    self.router.record_success(target.key)
                    response = LLMResponse(
                        request_id=request_id,
                        model=target.key,
                        content=content,
                        parsed=parsed,
                        usage=usage,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        attempts=attempts,
                    )
                    await self.record_trace(
                        request_id,
                        requested_model,
                        target.key,
                        request.prompt.name if request.prompt else None,
                        request.prompt.version if request.prompt else None,
                        usage,
                        response.latency_ms,
                        attempts,
                        "success",
                        target=target,
                        endpoint=endpoint,
                    )
                    return response
                except GatewayError:
                    # 内容/配置类错误：重试与切换目标都无意义，直接失败。
                    self.router.record_failure(target.key)
                    raise
                except Exception as exc:
                    last_error = exc
                    self.router.record_failure(target.key)
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
            endpoint=endpoint,
        )
        raise GatewayError(
            "model_unavailable", f"档位 {requested_model} 的全部路由目标均不可用"
        ) from last_error

    async def stream(self, request: LLMRequest, endpoint: str = "chat_completions") -> AsyncIterator[dict[str, Any]]:
        # 流式领域事件（协议无关）：content.delta / response.completed / response.failed。
        # 上游首块前可切换候选目标；首块后仅发流内错误，避免文本重复。
        messages = await self.build_messages(request)
        started = time.perf_counter()
        attempts = 0
        emitted = False
        last_error: Exception | None = None
        request_id = str(uuid.uuid4())
        try:
            candidates = self.resolve_candidates(request)
        except GatewayError as exc:
            # 预检与选路之间目标全部熔断等竞态：以流内失败事件收尾。
            yield {"type": "response.failed", "error": exc.code, "request_id": request_id}
            return
        for target in candidates:
            try:
                attempts += 1
                async for delta in self.provider.stream(target, messages, request.timeout_seconds):
                    emitted = True
                    yield {"type": "content.delta", "delta": delta, "model": target.key, "request_id": request_id}
                self.router.record_success(target.key)
                await self.record_trace(
                    request_id,
                    request.model,
                    target.key,
                    request.prompt.name if request.prompt else None,
                    request.prompt.version if request.prompt else None,
                    Usage(input_tokens=0, output_tokens=0),
                    int((time.perf_counter() - started) * 1000),
                    attempts,
                    "success",
                    target=target,
                    cost_usd=0.0,
                    endpoint=endpoint,
                )
                yield {"type": "response.completed", "model": target.key, "request_id": request_id}
                return
            except Exception as exc:
                last_error = exc
                self.router.record_failure(target.key)
                if emitted:
                    break
                # 首块前失败（无论是否可重试）都切换下一健康候选，与非流式行为一致
                continue
        logger.exception("upstream stream failed", exc_info=last_error)
        await self.record_trace(
            request_id,
            request.model,
            None,
            request.prompt.name if request.prompt else None,
            request.prompt.version if request.prompt else None,
            Usage(input_tokens=0, output_tokens=0),
            int((time.perf_counter() - started) * 1000),
            attempts,
            "failed",
            "upstream_stream_failed",
            endpoint=endpoint,
        )
        yield {"type": "response.failed", "error": "upstream_stream_failed", "request_id": request_id}

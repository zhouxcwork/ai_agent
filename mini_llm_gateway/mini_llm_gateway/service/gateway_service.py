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
from mini_llm_gateway.provider.base import (
    ContentRefusedError,
    FinishReason,
    Provider,
    RetryableUpstreamError,
    UpstreamID,
)
from mini_llm_gateway.repository.prompt_repository import PromptRepository
from mini_llm_gateway.repository.trace_repository import TraceRepository
from mini_llm_gateway.schemas.llm import LLMRequest, LLMResponse, Message, Usage
from mini_llm_gateway.schemas.trace import CallTrace
from mini_llm_gateway.service.limiter import Limiter
from mini_llm_gateway.service.model_router import ModelRouter
from mini_llm_gateway.service.repair import extract_json
from mini_llm_gateway.service.syntax_monitor import JsonSyntaxBroken, JsonSyntaxMonitor

logger = logging.getLogger("llm_gateway")

# 内容类错误码：不回报熔断（ADR 0009）
_CONTENT_ERROR_CODES = {"output.invalid_json", "output.schema_validation_failed"}


def is_retryable(exc: Exception) -> bool:
    # 只认内部统一的瞬态异常（供应商 SDK 异常已在适配层翻译），service 不感知 openai。
    # UpstreamRejectedError（401/403/404）不在其列：不可原地重试，由外层换下一候选。
    return isinstance(exc, (RetryableUpstreamError, TimeoutError, ConnectionError))


class GatewayService:
    # Gateway 业务编排：模板渲染、档位路由、候选 fallback、成本计算、熔断回报、trace 落库。
    def __init__(
        self,
        config: GatewayConfig,
        router: ModelRouter,
        providers: dict[str, Provider],
        prompt_repository: PromptRepository,
        trace_repository: TraceRepository,
        limiter: Limiter,
    ) -> None:
        self.config = config
        self.router = router
        self.providers = providers
        self.prompts = prompt_repository
        self.traces = trace_repository
        self.limiter = limiter

    def _adapter(self, target: ResolvedTarget) -> Provider:
        # ADR 0013：按路由目标的协议端点选择适配器，屏蔽协议差异
        adapter = self.providers.get(target.protocol)
        if adapter is None:
            raise GatewayError("platform.misconfigured", f"协议 {target.protocol} 没有可用适配器", 503)
        return adapter

    def calculate_cost(self, target: ResolvedTarget, usage: Usage) -> float:
        # 按实际命中路由目标的单价计算本次调用成本（ADR 0012）：
        # cached 部分按缓存价（价格表未配置 cached 键时退化为输入全价）。
        price = target.price_per_million
        cached = min(usage.cached_tokens, usage.input_tokens)
        input_cost = (usage.input_tokens - cached) * price.get("input", 0.0)
        cached_cost = cached * price.get("cached", price.get("input", 0.0))
        output_cost = usage.output_tokens * price.get("output", 0.0)
        return (input_cost + cached_cost + output_cost) / 1_000_000

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
        status: Literal["success", "failed", "refused", "cancelled"],
        error_code: str | None = None,
        cost_usd: float | None = None,
        target: ResolvedTarget | None = None,
        endpoint: str = "chat_completions",
        run_id: str | None = None,
        ttft_ms: int | None = None,
        finish_reason: str | None = None,
        upstream_request_id: str | None = None,
    ) -> None:
        # 留存调用元数据；trace 写库失败只降级为日志，不影响主请求。
        trace = CallTrace(
            request_id=request_id,
            timestamp=datetime.now(timezone.utc),
            endpoint=endpoint,
            run_id=run_id,
            requested_model=requested_model,
            actual_model=actual_model,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            cost_usd=cost_usd if cost_usd is not None else (self.calculate_cost(target, usage) if target else 0.0),
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            finish_reason=finish_reason,
            upstream_request_id=upstream_request_id,
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

    def resolve_candidates(self, request: LLMRequest, request_id: str | None = None) -> list[ResolvedTarget]:
        # 档位解析 + 健康度过滤 + 结构化能力过滤；无可用目标时抛出网关错误。
        return self.router.candidates(
            request.model, structured=request.response_schema is not None, request_id=request_id
        )

    async def validate_request(self, request: LLMRequest) -> None:
        # 流式入口的预校验：档位/健康候选/模板问题以 HTTP 状态码暴露而非流内事件；
        # 不推进轮询计数器，避免与真正选路重复消耗轮转位置。
        structured = request.response_schema is not None
        if not self.router.has_candidates(request.model, structured=structured):
            raise GatewayError("route.no_healthy_route", f"档位 {request.model} 没有健康且能力匹配的路由目标", 503)
        await self.build_messages(request)

    async def call(self, request: LLMRequest, endpoint: str = "chat_completions") -> LLMResponse:
        # 逐候选目标尝试：每目标有限重试，失败切换下一健康候选并回报熔断器。
        requested_model = request.model
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        attempts = 0
        last_error: Exception | None = None
        messages = await self.build_messages(request)
        candidates = self.resolve_candidates(request, request_id)
        target_busy = False
        for target in candidates:
            if not self.limiter.try_acquire_target(target.key):
                target_busy = True  # 路由目标并发已满：跳过该目标（ADR 0011，按模型粒度）
                continue
            try:
                for retry_number in range(self.config.retries_per_target + 1):
                    attempts += 1
                    try:
                        content, usage, upstream_id = await self._adapter(target).complete(
                            target, messages, request.timeout_seconds, request.response_schema
                        )
                        parsed: dict[str, Any] | list[Any] | None = None
                        if request.response_schema is not None:
                            # 修复链（ADR 0009）：先原始解析，失败做无损提取修复；
                            # 仍失败按 max_retries 重试上游（同目标），耗尽返回明确错误。
                            last_code = "output.invalid_json"
                            for structured_attempt in range(1 + self.config.structured_output.max_retries):
                                if structured_attempt > 0:
                                    attempts += 1
                                    content, usage, upstream_id = await self._adapter(target).complete(
                                        target, messages, request.timeout_seconds, request.response_schema
                                    )
                                parsed, content, last_code = self._parse_with_repair(
                                    content, request.response_schema
                                )
                                if last_code is None:
                                    break
                            if last_code is not None:
                                message = (
                                    "模型没有返回合法 JSON"
                                    if last_code == "output.invalid_json"
                                    else "模型结果不符合 response_schema"
                                )
                                await self.record_trace(
                                    request_id, requested_model, None,
                                    request.prompt.name if request.prompt else None,
                                    request.prompt.version if request.prompt else None,
                                    Usage(input_tokens=0, output_tokens=0),
                                    int((time.perf_counter() - started) * 1000), attempts,
                                    "failed", last_code, endpoint=endpoint,
                                )
                                raise GatewayError(last_code, message)
                        self.router.record_success(target.key)
                        response = LLMResponse(
                            request_id=request_id,
                            model=target.key,
                            content=content,
                            parsed=parsed,
                            usage=usage,
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            attempts=attempts,
                            upstream_request_id=upstream_id,
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
                            run_id=request.run_id,
                            finish_reason=response.finish_reason,
                            upstream_request_id=upstream_id,
                        )
                        return response
                    except GatewayError as exc:
                        # 内容类错误不回报熔断（ADR 0009：JSON 质量不是目标健康问题）；
                        # 配置类（如密钥缺失）代表目标不可用，仍计入熔断。
                        if exc.code not in _CONTENT_ERROR_CODES:
                            self.router.record_failure(target.key)
                        raise
                    except ContentRefusedError:
                        # 拒答（ADR 0010）：不重试、不换候选绕过安全策略、不计熔断；
                        # 以 OpenAI 方言语义透传（200 + finish_reason=content_filter）。
                        await self.record_trace(
                            request_id, requested_model, target.key,
                            request.prompt.name if request.prompt else None,
                            request.prompt.version if request.prompt else None,
                            Usage(input_tokens=0, output_tokens=0),
                            int((time.perf_counter() - started) * 1000), attempts,
                            "refused", "content.refused", target=target, endpoint=endpoint,
                            run_id=request.run_id, finish_reason="content_filter",
                        )
                        return LLMResponse(
                            request_id=request_id,
                            model=target.key,
                            content="",
                            usage=Usage(input_tokens=0, output_tokens=0),
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            attempts=attempts,
                            finish_reason="content_filter",
                        )
                    except Exception as exc:
                        # UpstreamRejectedError（401/403/404）等不可重试异常在此 break 换下一候选
                        last_error = exc
                        self.router.record_failure(target.key)
                        if is_retryable(exc) and retry_number < self.config.retries_per_target:
                            await asyncio.sleep(0.2 * 2 ** retry_number)  # 指数退避：0.2s / 0.4s（ADR 0014）
                            continue
                        break
            finally:
                self.limiter.release_target(target.key)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if attempts == 0:
            # 候选全部因路由目标并发被占，未发生任何真实尝试
            await self.record_trace(
                request_id, requested_model, None,
                request.prompt.name if request.prompt else None,
                request.prompt.version if request.prompt else None,
                Usage(input_tokens=0, output_tokens=0), latency_ms, attempts,
                "failed", "resource.target_busy", endpoint=endpoint, run_id=request.run_id,
            )
            raise GatewayError(
                "resource.target_busy", f"档位 {requested_model} 的全部路由目标并发已满", 429
            )
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
            "route.model_unavailable",
            endpoint=endpoint,
            run_id=request.run_id,
        )
        raise GatewayError(
            "route.model_unavailable", f"档位 {requested_model} 的全部路由目标均不可用", 503
        ) from last_error

    @staticmethod
    def _parse_with_repair(
        content: str, response_schema: dict[str, Any]
    ) -> tuple[dict[str, Any] | list[Any] | None, str, str | None]:
        # 原文与无损提取各试一次：返回 (parsed, 生效的 content, None) 或 (None, 原文, 错误码)
        last_code = "output.invalid_json"
        for candidate in (content, extract_json(content)):
            try:
                parsed = json.loads(candidate)
                validate(instance=parsed, schema=response_schema)
                return parsed, candidate, None
            except json.JSONDecodeError:
                last_code = "output.invalid_json"
            except JsonSchemaError:
                last_code = "output.schema_validation_failed"
        return None, content, last_code

    async def stream(self, request: LLMRequest, endpoint: str = "chat_completions") -> AsyncIterator[dict[str, Any]]:
        # 流式领域事件（协议无关）：content.delta / response.completed / response.failed。
        # 上游首块前可切换候选目标；首块后仅发流内错误，避免文本重复。
        # 结构化流式（ADR 0008）：流中语法监控破损即断流，流尾做完整 JSON 解析 + Schema 裁决。
        # 流式 usage（决策 16）：上游 include_usage 末块并入 completed 事件与 trace。
        # 取消终态（决策 17）：客户端断连以 cancelled 状态留痕后继续传播取消。
        messages = await self.build_messages(request)
        started = time.perf_counter()
        attempts = 0
        emitted = False
        last_error: Exception | None = None
        request_id = str(uuid.uuid4())
        structured = request.response_schema is not None
        stream_usage = Usage(input_tokens=0, output_tokens=0)
        last_target: ResolvedTarget | None = None
        completed = False
        first_delta_at: float | None = None  # 首个业务 delta 时刻（TTFT 口径，ADR 0012）
        upstream_id: str | None = None

        def _ttft_ms() -> int | None:
            return int((first_delta_at - started) * 1000) if first_delta_at is not None else None

        def prompt_meta() -> tuple[str | None, str | None]:
            return (
                request.prompt.name if request.prompt else None,
                request.prompt.version if request.prompt else None,
            )

        def failed_event(code: str) -> dict[str, Any]:
            return {"type": "response.failed", "error": code, "request_id": request_id}

        async def record_failed(code: str) -> None:
            prompt_name, prompt_version = prompt_meta()
            await self.record_trace(
                request_id, request.model, None, prompt_name, prompt_version, stream_usage,
                int((time.perf_counter() - started) * 1000), attempts, "failed", code, endpoint=endpoint,
                run_id=request.run_id, ttft_ms=_ttft_ms(),
            )

        try:
            try:
                candidates = self.resolve_candidates(request, request_id)
            except GatewayError as exc:
                # 预检与选路之间目标全部熔断等竞态：以流内失败事件收尾。
                yield failed_event(exc.code)
                return
            abort = False
            for target in candidates:
                if not self.limiter.try_acquire_target(target.key):
                    continue  # 路由目标并发已满：跳过该目标，尝试下一候选（按模型粒度）
                try:
                    # 每目标重试次数走 retries_per_target 配置（不含初次，ADR 0014），与非流式一致；首块前可重试可切换
                    for retry_number in range(self.config.retries_per_target + 1):
                        monitor = JsonSyntaxMonitor() if structured else None
                        collected: list[str] = []  # 结构化时收集增量做流尾终审
                        last_target = target
                        refused = False
                        finish_reason = "stop"
                        try:
                            attempts += 1
                            async for item in self._adapter(target).stream(
                                target, messages, request.timeout_seconds, request.response_schema
                            ):
                                if isinstance(item, Usage):
                                    stream_usage = item  # 上游 include_usage 末块
                                    continue
                                if isinstance(item, FinishReason):
                                    if item.value == "content_filter":
                                        refused = True
                                    else:
                                        finish_reason = item.value
                                    continue
                                if isinstance(item, UpstreamID):
                                    upstream_id = item.value
                                    continue
                                delta = item
                                if first_delta_at is None:
                                    first_delta_at = time.perf_counter()
                                emitted = True
                                if monitor is not None:
                                    try:
                                        monitor.feed(delta)
                                    except JsonSyntaxBroken:
                                        # 语法破损：立即断流（return 关闭上游生成器），省 token 快速失败
                                        await record_failed("output.invalid_json")
                                        yield failed_event("output.invalid_json")
                                        return
                                    collected.append(delta)
                                yield {"type": "content.delta", "delta": delta, "model": target.key, "request_id": request_id}
                            if refused:
                                # 拒答（ADR 0010）：不重试、不换候选、不计熔断，跳过结构化终审，
                                # 正常收流但 finish_reason=content_filter 透传调用方。
                                prompt_name, prompt_version = prompt_meta()
                                await self.record_trace(
                                    request_id, request.model, target.key, prompt_name, prompt_version, stream_usage,
                                    int((time.perf_counter() - started) * 1000), attempts,
                                    "refused", "content.refused", target=target, endpoint=endpoint,
                                    run_id=request.run_id, ttft_ms=_ttft_ms(),
                                    finish_reason="content_filter", upstream_request_id=upstream_id,
                                )
                                completed = True
                                yield {
                                    "type": "response.completed",
                                    "model": target.key,
                                    "request_id": request_id,
                                    "finish_reason": "content_filter",
                                    "usage": {
                                        "input_tokens": stream_usage.input_tokens,
                                        "output_tokens": stream_usage.output_tokens,
                                    },
                                }
                                return
                            if structured:
                                code = self._final_validate("".join(collected), request.response_schema)
                                if code is not None:
                                    await record_failed(code)
                                    yield failed_event(code)
                                    return
                            self.router.record_success(target.key)
                            prompt_name, prompt_version = prompt_meta()
                            await self.record_trace(
                                request_id, request.model, target.key, prompt_name, prompt_version, stream_usage,
                                int((time.perf_counter() - started) * 1000), attempts,
                                "success", target=target, endpoint=endpoint,
                                run_id=request.run_id, ttft_ms=_ttft_ms(),
                                finish_reason=finish_reason, upstream_request_id=upstream_id,
                            )
                            completed = True
                            yield {
                                "type": "response.completed",
                                "model": target.key,
                                "request_id": request_id,
                                "finish_reason": finish_reason,
                                "usage": {
                                    "input_tokens": stream_usage.input_tokens,
                                    "output_tokens": stream_usage.output_tokens,
                                },
                            }
                            return
                        except Exception as exc:
                            last_error = exc
                            self.router.record_failure(target.key)
                            if emitted:
                                abort = True  # 首块后不重试不切换，避免文本重复
                                break
                            if is_retryable(exc) and retry_number < self.config.retries_per_target:
                                await asyncio.sleep(0.2 * 2 ** retry_number)  # 指数退避：0.2s / 0.4s（ADR 0014）
                                continue  # 首块前同目标重试
                            break  # 切换下一健康候选
                finally:
                    self.limiter.release_target(target.key)
                if abort:
                    break
            if attempts == 0:
                # 候选全部因路由目标并发被占，未发生任何真实尝试
                await record_failed("resource.target_busy")
                yield failed_event("resource.target_busy")
                return
            logger.exception("upstream stream failed", exc_info=last_error)
            await record_failed("upstream.stream_failed")
            yield failed_event("upstream.stream_failed")
        except (asyncio.CancelledError, GeneratorExit):
            # 客户端断连/任务取消：cancelled 终态留痕（trace 失败只降级日志），再传播取消
            if not completed:
                try:
                    prompt_name, prompt_version = prompt_meta()
                    await asyncio.shield(
                        self.record_trace(
                            request_id, request.model,
                            last_target.key if last_target else None,
                            prompt_name, prompt_version, stream_usage,
                            int((time.perf_counter() - started) * 1000), attempts,
                            "cancelled", "stream.client_cancelled",
                            target=last_target, endpoint=endpoint,
                            run_id=request.run_id, ttft_ms=_ttft_ms(),
                        )
                    )
                except Exception:
                    logger.exception("cancelled trace 写入失败 request_id=%s", request_id)
            raise

    @staticmethod
    def _final_validate(content: str, response_schema: dict[str, Any] | None) -> str | None:
        # 流尾结构化终审：合法返回 None，否则返回错误码
        try:
            parsed = json.loads(content)
            if response_schema is not None:
                validate(instance=parsed, schema=response_schema)
            return None
        except json.JSONDecodeError:
            return "output.invalid_json"
        except JsonSchemaError:
            return "output.schema_validation_failed"

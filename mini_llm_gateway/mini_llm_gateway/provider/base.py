from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from mini_llm_gateway.config import ResolvedTarget
from mini_llm_gateway.schemas.llm import Message, Usage


class RetryableUpstreamError(Exception):
    """供应商瞬态故障的统一内部异常（连接/超时/限流），service 层据此重试与切换候选，
    不感知任何供应商 SDK 的异常类型。"""


class Provider(Protocol):
    # 规定供应商 Adapter 的统一接口，业务流程不依赖具体 SDK。
    # stream 末尾 yield 一个 Usage 对象（上游 include_usage 的末块用量）。

    async def complete(
        self,
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None,
    ) -> tuple[str, Usage]: ...

    def stream(
        self,
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None = None,
    ) -> AsyncIterator[str | Usage]: ...

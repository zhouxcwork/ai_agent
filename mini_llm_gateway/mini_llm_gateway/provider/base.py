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
    ) -> AsyncIterator[str]: ...

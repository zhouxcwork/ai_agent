from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from mini_llm_gateway.config import ModelConfig
from mini_llm_gateway.schemas.llm import Message, Usage


class Provider(Protocol):
    # 规定供应商 Adapter 的统一接口，业务流程不依赖具体 SDK。
    async def complete(
        self,
        config: ModelConfig,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None,
    ) -> tuple[str, Usage]: ...

    def stream(
        self,
        config: ModelConfig,
        messages: list[Message],
        timeout_seconds: float,
    ) -> AsyncIterator[str]: ...

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from mini_llm_gateway.config import ResolvedTarget
from mini_llm_gateway.schemas.llm import Message, Usage


class RetryableUpstreamError(Exception):
    """供应商瞬态故障的统一内部异常（连接/超时/429/5xx），service 层据此重试与切换候选，
    不感知任何供应商 SDK 的异常类型。"""


class UpstreamRejectedError(Exception):
    """供应商明确拒绝当前目标（key 无效/无权限/模型不存在）：不可原地重试，
    可切换下一候选，计入熔断。"""


class ContentRefusedError(Exception):
    """模型因安全策略拒答（finish_reason=content_filter）：不重试、不换候选、不计熔断，
    以 OpenAI 方言语义透传给调用方。"""


class FinishReason:
    """流式末块的上游结束原因（stop/length/content_filter 等），供领域事件透传。"""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


class UpstreamID:
    """供应商侧请求 ID 信号：非流式随返回值携带，流式作为首个信号 yield，供 trace 留痕。"""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


class Provider(Protocol):
    # 规定供应商 Adapter 的统一接口，业务流程不依赖具体 SDK。
    # complete 返回 (content, usage, upstream_request_id)；stream 依次 yield
    # UpstreamID（首块前一次）、str 增量、FinishReason、Usage（include_usage 末块用量）。

    async def complete(
        self,
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None,
    ) -> tuple[str, Usage, str | None]: ...

    def stream(
        self,
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None = None,
    ) -> AsyncIterator[str | Usage | FinishReason | UpstreamID]: ...

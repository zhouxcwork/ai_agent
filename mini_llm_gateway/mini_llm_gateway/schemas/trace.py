from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CallTrace(BaseModel):
    # 保存单次调用的模型、Token、成本、延迟与状态，默认不记录文本内容。
    model_config = ConfigDict(extra="forbid")

    request_id: str
    timestamp: datetime
    endpoint: str = "chat_completions"  # 调用来源端点：chat_completions / responses
    run_id: str | None = None  # 调用方业务链路标识（X-Run-Id 头透传）
    requested_model: str
    actual_model: str | None = None
    prompt_name: str | None = None
    prompt_version: str | None = None
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(default=0, ge=0)  # input 中命中上游缓存的部分
    reasoning_tokens: int = Field(default=0, ge=0)  # 推理型模型思维链开销（含在 output 内）
    cost_usd: float = Field(ge=0)
    latency_ms: int = Field(ge=0)
    ttft_ms: int | None = None  # 首 Token 延迟（ADR 0012）：首个业务 delta 距请求开始的毫秒数；非流式为空
    finish_reason: str | None = None  # stop / content_filter / length 等
    upstream_request_id: str | None = None  # 供应商侧请求 ID
    attempts: int = Field(ge=0)
    status: Literal["success", "failed", "refused", "cancelled"]
    error_code: str | None = None

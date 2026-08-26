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
    requested_model: str
    actual_model: str | None = None
    prompt_name: str | None = None
    prompt_version: str | None = None
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    latency_ms: int = Field(ge=0)
    attempts: int = Field(ge=0)
    status: Literal["success", "failed"]
    error_code: str | None = None

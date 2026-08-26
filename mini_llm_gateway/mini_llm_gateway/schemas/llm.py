from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    # 定义跨模型通用的单条对话消息，隔离供应商消息格式差异。
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)


class PromptSelection(BaseModel):
    # 只允许调用方选择受控模板及变量，不能提交或覆盖模板正文。
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=50)
    variables: dict[str, str] = Field(default_factory=dict)


class LLMRequest(BaseModel):
    # 统一 Gateway 请求协议，并在 HTTP 入口拦截不合法组合和字段。
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=100)
    messages: list[Message] = Field(min_length=1, max_length=100)
    stream: bool = False
    response_schema: dict[str, Any] | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    prompt: PromptSelection | None = None
    tenant_id: str | None = None  # 认证依赖注入的租户归属；未启用认证时为 None
    run_id: str | None = None  # 调用方业务链路标识（X-Run-Id 头透传），仅透录不解释
    # 流式与结构化组合已解禁（ADR 0008）：流中语法监控、流尾 Schema 裁决


class Usage(BaseModel):
    # 统一输入/输出 Token 统计口径，用于成本和用量治理。
    # cached_tokens 是 input 中命中上游缓存的部分（已含在 input_tokens 内）；
    # reasoning_tokens 是推理型模型的思维链开销（已含在 output_tokens 内）。
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)


class LLMResponse(BaseModel):
    # 统一模型调用结果，并作为 FastAPI 响应出口的 Pydantic 校验契约。
    model_config = ConfigDict(extra="forbid")

    request_id: str
    model: str
    content: str
    parsed: dict[str, Any] | list[Any] | None = None
    usage: Usage
    latency_ms: int = Field(ge=0)
    attempts: int = Field(ge=1)
    finish_reason: str = "stop"  # content_filter = 模型安全拒答（ADR 0010，200 透传）
    upstream_request_id: str | None = None  # 供应商侧请求 ID，排障时定位上游日志

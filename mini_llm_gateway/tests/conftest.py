from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mini_llm_gateway.app import create_app
from mini_llm_gateway.config import (
    AdminConfig,
    AuthConfig,
    CircuitBreakerConfig,
    DatabaseConfig,
    GatewayConfig,
    LimitsConfig,
    ModeConfig,
    ProviderConfig,
    ResolvedTarget,
    RouteTarget,
    StructuredOutputConfig,
)
from mini_llm_gateway.provider.base import ContentRefusedError, FinishReason, UpstreamID
from mini_llm_gateway.schemas.llm import Message, Usage

ADMIN_TOKEN_ENV = "TEST_ADMIN_TOKEN"

PRIMARY_MODEL = "primary-model"
BACKUP_MODEL = "backup-model"
PRIMARY_KEY = f"p1/{PRIMARY_MODEL}"
BACKUP_KEY = f"p2/{BACKUP_MODEL}"


class FakeProvider:
    # 测试用供应商替身：可脚本化指定各供应商模型的返回内容或抛出可重试异常。
    def __init__(self) -> None:
        self.contents: dict[str, str] = {}
        self.fail_models: set[str] = set()
        self.refuse_models: set[str] = set()  # 安全拒答：非流式抛 ContentRefusedError，流式标 content_filter
        self.complete_calls: list[str] = []
        self.stream_calls: list[str] = []
        self.last_messages: list[Message] = []
        self.stream_chunks: list[str] | None = None  # None 时用默认增量 ["你", "好"]
        self.delay_seconds: float = 0.0  # 模拟上游耗时，供并发限流测试制造在途窗口
        self.usage = Usage(input_tokens=10, output_tokens=5)  # 可覆写以测试 usage 细分与计价

    async def complete(
        self,
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None,
    ) -> tuple[str, Usage, str | None]:
        self.complete_calls.append(target.provider_model)
        self.last_messages = list(messages)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if target.provider_model in self.refuse_models:
            raise ContentRefusedError(f"refused by policy: {target.provider_model}")
        if target.provider_model in self.fail_models:
            raise TimeoutError(f"upstream {target.provider_model} timeout")
        return self.contents.get(target.provider_model, "ok"), self.usage, f"upstream-{target.provider_model}"

    async def stream(
        self,
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None = None,
    ) -> AsyncIterator[str | Usage | FinishReason | UpstreamID]:
        self.stream_calls.append(target.provider_model)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        yield UpstreamID(f"upstream-{target.provider_model}")
        if target.provider_model in self.refuse_models:
            yield "抱歉"
            yield FinishReason("content_filter")
            return
        if target.provider_model in self.fail_models:
            raise TimeoutError(f"upstream {target.provider_model} timeout")
        chunks = self.stream_chunks if self.stream_chunks is not None else ["你", "好"]
        for delta in chunks:
            yield delta
        yield FinishReason("stop")  # 模拟上游末块结束原因
        yield self.usage  # 模拟 include_usage 末块


def make_config(
    database_path: str,
    *,
    failure_threshold: int = 5,
    cooldown_seconds: float = 30.0,
    structured_max_retries: int = 2,
    auth_keys: dict[str, str] | None = None,
    limits: LimitsConfig | None = None,
) -> GatewayConfig:
    def target(provider: str, model: str, price_input: float) -> RouteTarget:
        return RouteTarget(
            provider=provider,
            model=model,
            supports_structured_output=True,
            structured_output_mode="json_object",
            price_per_million={"input": price_input, "output": price_input * 4},
        )

    return GatewayConfig(
        database=DatabaseConfig(path=database_path),
        admin=AdminConfig(token_env=ADMIN_TOKEN_ENV),
        auth=AuthConfig(api_keys=auth_keys or {}),
        limits=limits or LimitsConfig(),
        circuit_breaker=CircuitBreakerConfig(
            failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds
        ),
        structured_output=StructuredOutputConfig(max_retries=structured_max_retries),
        providers={
            "p1": ProviderConfig(base_url="http://p1.test", api_key_env="K1"),
            "p2": ProviderConfig(base_url="http://p2.test", api_key_env="K2"),
        },
        modes={
            "fast": ModeConfig(
                targets=[target("p1", PRIMARY_MODEL, 1.0), target("p2", BACKUP_MODEL, 0.8)]
            )
        },
    )


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def app(tmp_path, fake_provider, monkeypatch):
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "secret-token")
    return create_app(make_config(str(tmp_path / "gateway.db")), provider=fake_provider)


@pytest.fixture
def client(app) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def sdk(app):
    # 真实 openai SDK 直连网关（ASGI transport），验证"已有 SDK 无缝接入"。
    import httpx
    from openai import AsyncOpenAI

    async with app.router.lifespan_context(app):
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        # 关闭 SDK 自动重试：5xx 会被 SDK 默认重试 2 次，干扰网关重试行为的断言
        yield AsyncOpenAI(api_key="dummy", base_url="http://testserver/v1", http_client=http, max_retries=0)
        await http.aclose()

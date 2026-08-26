from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mini_llm_gateway.app import create_app
from mini_llm_gateway.config import (
    AdminConfig,
    CircuitBreakerConfig,
    DatabaseConfig,
    GatewayConfig,
    ModeConfig,
    ProviderConfig,
    ResolvedTarget,
    RouteTarget,
)
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
        self.complete_calls: list[str] = []
        self.last_messages: list[Message] = []

    async def complete(
        self,
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None,
    ) -> tuple[str, Usage]:
        self.complete_calls.append(target.provider_model)
        self.last_messages = list(messages)
        if target.provider_model in self.fail_models:
            raise TimeoutError(f"upstream {target.provider_model} timeout")
        return self.contents.get(target.provider_model, "ok"), Usage(input_tokens=10, output_tokens=5)

    async def stream(
        self,
        target: ResolvedTarget,
        messages: list[Message],
        timeout_seconds: float,
    ) -> AsyncIterator[str]:
        if target.provider_model in self.fail_models:
            raise TimeoutError(f"upstream {target.provider_model} timeout")
        for delta in ["你", "好"]:
            yield delta


def make_config(
    database_path: str, *, failure_threshold: int = 5, cooldown_seconds: float = 30.0
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
        circuit_breaker=CircuitBreakerConfig(
            failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds
        ),
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
        yield AsyncOpenAI(api_key="dummy", base_url="http://testserver/v1", http_client=http)
        await http.aclose()

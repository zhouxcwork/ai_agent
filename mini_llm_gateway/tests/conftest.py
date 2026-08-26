from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mini_llm_gateway.app import create_app
from mini_llm_gateway.config import AdminConfig, DatabaseConfig, GatewayConfig, ModelConfig
from mini_llm_gateway.schemas.llm import Message, Usage

ADMIN_TOKEN_ENV = "TEST_ADMIN_TOKEN"


class FakeProvider:
    # 测试用供应商替身：可脚本化指定各供应商模型的返回内容或抛出可重试异常。
    def __init__(self) -> None:
        self.contents: dict[str, str] = {}
        self.fail_models: set[str] = set()
        self.complete_calls: list[str] = []

    async def complete(
        self,
        config: ModelConfig,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None,
    ) -> tuple[str, Usage]:
        self.complete_calls.append(config.provider_model)
        if config.provider_model in self.fail_models:
            raise TimeoutError(f"upstream {config.provider_model} timeout")
        return self.contents.get(config.provider_model, "ok"), Usage(input_tokens=10, output_tokens=5)

    async def stream(
        self,
        config: ModelConfig,
        messages: list[Message],
        timeout_seconds: float,
    ) -> AsyncIterator[str]:
        if config.provider_model in self.fail_models:
            raise TimeoutError(f"upstream {config.provider_model} timeout")
        for delta in ["你", "好"]:
            yield delta


def make_config(database_path: str) -> GatewayConfig:
    def model(provider_model: str, price_input: float) -> ModelConfig:
        return ModelConfig(
            provider_model=provider_model,
            base_url="http://upstream.test",
            api_key_env="UNUSED_TEST_KEY",
            supports_structured_output=True,
            structured_output_mode="json_object",
            price_per_million={"input": price_input, "output": price_input * 4},
        )

    return GatewayConfig(
        database=DatabaseConfig(path=database_path),
        admin=AdminConfig(token_env=ADMIN_TOKEN_ENV),
        fallback_model="backup",
        models={
            "primary": model("p1", 1.0),
            "backup": model("b1", 0.8),
        },
    )


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def client(tmp_path, fake_provider, monkeypatch) -> TestClient:
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "secret-token")
    app = create_app(make_config(str(tmp_path / "gateway.db")), provider=fake_provider)
    with TestClient(app) as test_client:
        yield test_client

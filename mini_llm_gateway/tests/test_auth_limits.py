from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from mini_llm_gateway.app import create_app
from mini_llm_gateway.config import (
    LimitsConfig,
    ModeConfig,
    RouteTarget,
    TargetLimitConfig,
    TenantLimitConfig,
)
from tests.conftest import BACKUP_KEY, PRIMARY_KEY, make_config

BODY = {"model": "fast", "messages": [{"role": "user", "content": "hi"}]}


def _client(tmp_path, fake_provider, **kwargs) -> TestClient:
    app = create_app(make_config(str(tmp_path / "gw.db"), **kwargs), provider=fake_provider)
    return TestClient(app)


def test_auth_disabled_allows_anonymous(tmp_path, fake_provider):
    # 白名单为空 = 不启用认证：无 Authorization 头也能调用（本地开发模式）
    with _client(tmp_path, fake_provider) as client:
        assert client.post("/v1/chat/completions", json=BODY).status_code == 200


def test_auth_invalid_key_rejected(tmp_path, fake_provider):
    with _client(tmp_path, fake_provider, auth_keys={"secret-key": "tenant-a"}) as client:
        response = client.post("/v1/chat/completions", json=BODY)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "auth.invalid_key"

        response = client.post(
            "/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer wrong-key"}
        )
        assert response.status_code == 401

        response = client.post(
            "/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer secret-key"}
        )
        assert response.status_code == 200


def test_tenant_rate_limited_after_burst(tmp_path, fake_provider):
    # 令牌桶容量 1：首个请求消耗唯一 token，第二个请求立即 429
    limits = LimitsConfig(default=TenantLimitConfig(qps=1, burst=1, max_concurrency=5))
    with _client(tmp_path, fake_provider, limits=limits) as client:
        assert client.post("/v1/chat/completions", json=BODY).status_code == 200
        response = client.post("/v1/chat/completions", json=BODY)
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "resource.tenant_rate_limited"


@pytest.mark.asyncio
async def test_tenant_concurrency_limit_rejects_second(tmp_path, fake_provider):
    # 并发 1：上游延迟制造在途窗口，第二个并发请求 429 tenant_busy
    fake_provider.delay_seconds = 0.2
    limits = LimitsConfig(default=TenantLimitConfig(qps=100, burst=100, max_concurrency=1))
    app = create_app(make_config(str(tmp_path / "gw.db"), limits=limits), provider=fake_provider)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            first, second = await asyncio.gather(
                http.post("/v1/chat/completions", json=BODY),
                http.post("/v1/chat/completions", json=BODY),
            )
        assert first.status_code == 200
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "resource.tenant_busy"


def test_target_busy_skips_to_next_candidate(tmp_path, fake_provider):
    # 主选路由目标并发占满：请求跳过它，由下一候选承接（路由目标切换只记 Trace，不透出响应）
    limits = LimitsConfig(targets={PRIMARY_KEY: TargetLimitConfig(max_concurrency=1)})
    with _client(tmp_path, fake_provider, limits=limits) as client:
        assert client.app.state.limiter.try_acquire_target(PRIMARY_KEY)  # 手动占住唯一槽位
        response = client.post("/v1/chat/completions", json=BODY)
        assert response.status_code == 200
        assert "actual_model" not in response.json()
        assert fake_provider.complete_calls == ["backup-model"]


def test_all_targets_busy_rejected(tmp_path, fake_provider):
    limits = LimitsConfig(
        targets={
            PRIMARY_KEY: TargetLimitConfig(max_concurrency=1),
            BACKUP_KEY: TargetLimitConfig(max_concurrency=1),
        }
    )
    with _client(tmp_path, fake_provider, limits=limits) as client:
        limiter = client.app.state.limiter
        limiter.try_acquire_target(PRIMARY_KEY)
        limiter.try_acquire_target(BACKUP_KEY)
        response = client.post("/v1/chat/completions", json=BODY)
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "resource.target_busy"
        assert fake_provider.complete_calls == []  # 未发生任何真实上游尝试


def test_target_qps_rate_limits_per_model(tmp_path, fake_provider):
    # 目标级 QPS（按模型独立限流）：qps=1 时同一目标连续第二次立即被拒；未配 qps 的目标不受影响
    limits = LimitsConfig(
        targets={
            PRIMARY_KEY: TargetLimitConfig(max_concurrency=5, qps=1),
        }
    )
    with _client(tmp_path, fake_provider, limits=limits) as client:
        limiter = client.app.state.limiter
        assert limiter.try_acquire_target(PRIMARY_KEY)
        assert not limiter.try_acquire_target(PRIMARY_KEY)  # 突发额度耗尽，未到下一秒
        assert limiter.try_acquire_target(BACKUP_KEY)  # 未配置 = 不限速
        assert limiter.try_acquire_target(BACKUP_KEY)


def test_target_limits_isolate_models_of_same_provider(tmp_path, fake_provider):
    # 模型粒度隔离：同供应商的两个模型各自计槽，flash 被占满不影响 smart 档的 pro
    def _target(provider: str, model: str) -> RouteTarget:
        return RouteTarget(
            provider=provider, model=model, supports_structured_output=True,
            structured_output_mode="json_object", price_per_million={"input": 1.0, "output": 4.0},
        )

    config = make_config(
        str(tmp_path / "gw.db"),
        limits=LimitsConfig(targets={"p1/flash-model": TargetLimitConfig(max_concurrency=1)}),
    )
    # fast → p1/flash-model（并发 1，将被占满）；smart → p1/pro-model（同供应商，不受影响）
    config = config.model_copy(
        update={
            "modes": {
                "fast": ModeConfig(targets=[_target("p1", "flash-model")]),
                "smart": ModeConfig(targets=[_target("p1", "pro-model")]),
            }
        }
    )
    with TestClient(create_app(config, provider=fake_provider)) as client:
        assert client.app.state.limiter.try_acquire_target("p1/flash-model")
        busy = client.post("/v1/chat/completions", json=BODY)  # fast 唯一目标被占
        assert busy.status_code == 429
        assert busy.json()["error"]["code"] == "resource.target_busy"
        isolated = client.post(  # 同供应商另一模型正常服务
            "/v1/chat/completions", json={"model": "smart", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert isolated.status_code == 200
        assert any(t["actual_model"] == "p1/pro-model" for t in client.get("/v1/traces").json())

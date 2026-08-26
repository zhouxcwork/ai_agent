from __future__ import annotations

import pytest

from mini_llm_gateway.app import create_app
from tests.conftest import (
    ADMIN_TOKEN_ENV,
    BACKUP_KEY,
    BACKUP_MODEL,
    PRIMARY_KEY,
    PRIMARY_MODEL,
    make_config,
)

def make_fast_client(tmp_path, fake_provider, monkeypatch, **breaker):
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "secret-token")
    app = create_app(make_config(str(tmp_path / "gw.db"), **breaker), provider=fake_provider)
    return app


def test_models_endpoint_lists_modes_and_routes(client):
    body = client.get("/v1/models").json()
    assert body["object"] == "list"
    assert [m["id"] for m in body["data"]] == ["fast"]
    routes = body["gateway_routes"]["fast"]
    assert {r["target"] for r in routes} == {PRIMARY_KEY, BACKUP_KEY}
    assert all(r["healthy"] and r["state"] == "closed" for r in routes)


async def test_sdk_models_list(sdk):
    models = await sdk.models.list()
    ids = [m.id for m in models.data]
    assert "fast" in ids


def test_circuit_trips_and_traffic_shifts(tmp_path, fake_provider, monkeypatch):
    from fastapi.testclient import TestClient

    app = make_fast_client(tmp_path, fake_provider, monkeypatch, failure_threshold=2)
    with TestClient(app) as client:
        # 主选目标连续失败（每次调用 2 次尝试），阈值 2 → 两次调用后熔断
        fake_provider.fail_models = {PRIMARY_MODEL}
        for _ in range(2):
            assert client.post(
                "/v1/chat/completions",
                json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
            ).status_code == 200  # fallback 到备用目标

        routes = {r["target"]: r for r in client.get("/v1/models").json()["gateway_routes"]["fast"]}
        assert routes[PRIMARY_KEY]["state"] == "open"
        assert routes[PRIMARY_KEY]["healthy"] is False

        # 主选恢复，但熔断中：流量仍全部走备用目标
        fake_provider.fail_models = set()
        fake_provider.contents = {PRIMARY_MODEL: "from-primary"}
        body = client.post(
            "/v1/chat/completions",
            json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        ).json()
        assert body["actual_model"] == BACKUP_KEY

        # trace 记录的 fallback 事实
        traces = client.get("/v1/traces").json()
        assert any(t["actual_model"] == BACKUP_KEY for t in traces)


def test_circuit_recovers_via_half_open(tmp_path, fake_provider, monkeypatch):
    from fastapi.testclient import TestClient

    # cooldown=0：熔断后立即进入半开，下一次调用即试探
    app = make_fast_client(tmp_path, fake_provider, monkeypatch, failure_threshold=2, cooldown_seconds=0)
    with TestClient(app) as client:
        fake_provider.fail_models = {PRIMARY_MODEL}
        for _ in range(2):
            client.post(
                "/v1/chat/completions",
                json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
            )

        fake_provider.fail_models = set()
        body = client.post(
            "/v1/chat/completions",
            json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        ).json()
        assert body["actual_model"] == PRIMARY_KEY  # 半开试探成功 → 恢复

        routes = {r["target"]: r for r in client.get("/v1/models").json()["gateway_routes"]["fast"]}
        assert routes[PRIMARY_KEY]["state"] == "closed"


def test_all_targets_tripped_returns_503(tmp_path, fake_provider, monkeypatch):
    from fastapi.testclient import TestClient

    app = make_fast_client(tmp_path, fake_provider, monkeypatch, failure_threshold=2)
    with TestClient(app) as client:
        fake_provider.fail_models = {PRIMARY_MODEL, BACKUP_MODEL}
        for _ in range(2):
            client.post(
                "/v1/chat/completions",
                json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
            )
        response = client.post(
            "/v1/chat/completions",
            json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "route.no_healthy_route"

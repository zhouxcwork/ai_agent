from __future__ import annotations

import json


def test_call_primary_success(client, fake_provider):
    fake_provider.contents = {"p1": "hello from primary"}
    response = client.post(
        "/v1/llm",
        json={"model": "primary", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "primary"
    assert body["content"] == "hello from primary"
    assert body["attempts"] == 1


def test_fallback_to_backup_model(client, fake_provider):
    fake_provider.fail_models = {"p1"}
    fake_provider.contents = {"b1": "hello from backup"}
    response = client.post(
        "/v1/llm",
        json={"model": "primary", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "backup"
    assert body["attempts"] == 3  # 主模型重试 2 次 + 备用 1 次

    traces = client.get("/v1/traces").json()
    assert traces[0]["status"] == "success"
    assert traces[0]["actual_model"] == "backup"
    assert traces[0]["cost_usd"] > 0


def test_all_models_unavailable(client, fake_provider):
    fake_provider.fail_models = {"p1", "b1"}
    response = client.post(
        "/v1/llm",
        json={"model": "primary", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    assert response.json()["code"] == "model_unavailable"

    traces = client.get("/v1/traces").json()
    assert traces[0]["status"] == "failed"
    assert traces[0]["error_code"] == "model_unavailable"


def test_unknown_model_rejected(client):
    response = client.post(
        "/v1/llm",
        json={"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_model"


def test_stream_endpoint(client, fake_provider):
    fake_provider.contents = {"p1": "ignored"}
    with client.stream(
        "POST",
        "/v1/llm/stream",
        json={"model": "primary", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    ) as response:
        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]
    assert events[-1] == {"type": "response.completed", "model": "primary"}
    deltas = [e["delta"] for e in events if e["type"] == "content.delta"]
    assert "".join(deltas) == "你好"


def test_stream_with_schema_rejected_by_contract(client):
    response = client.post(
        "/v1/llm/stream",
        json={
            "model": "primary",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "response_schema": {"type": "object"},
        },
    )
    assert response.status_code == 422  # Pydantic 契约在入口拦截


def test_non_stream_endpoint_rejects_stream_flag(client):
    response = client.post(
        "/v1/llm",
        json={"model": "primary", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "use_stream_endpoint"


def test_structured_output_invalid_json(client, fake_provider):
    fake_provider.contents = {"p1": "这不是 JSON"}
    response = client.post(
        "/v1/llm",
        json={
            "model": "primary",
            "messages": [{"role": "user", "content": "hi"}],
            "response_schema": {"type": "object", "properties": {"a": {"type": "number"}}},
        },
    )
    assert response.status_code == 502
    assert response.json()["code"] == "invalid_json"


def test_structured_output_success(client, fake_provider):
    fake_provider.contents = {"p1": '{"a": 42}'}
    response = client.post(
        "/v1/llm",
        json={
            "model": "primary",
            "messages": [{"role": "user", "content": "hi"}],
            "response_schema": {"type": "object", "properties": {"a": {"type": "number"}}},
        },
    )
    assert response.status_code == 200
    assert response.json()["parsed"] == {"a": 42}


def test_call_with_prompt_template(client, fake_provider):
    fake_provider.contents = {"p1": "ok"}
    created = client.post(
        "/v1/prompts",
        json={"name": "kb", "version": "v1", "system_template": "你是{{product}}的决策器"},
        headers={"X-Admin-Token": "secret-token"},
    )
    assert created.status_code == 201
    response = client.post(
        "/v1/llm",
        json={
            "model": "primary",
            "prompt": {"name": "kb", "version": "v1", "variables": {"product": "知识库"}},
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 200
    trace = client.get("/v1/traces").json()[0]
    assert trace["prompt_name"] == "kb"
    assert trace["prompt_version"] == "v1"

from __future__ import annotations

from tests.conftest import PRIMARY_MODEL, BACKUP_MODEL


def test_all_targets_unavailable_records_failed_trace(client, fake_provider):
    fake_provider.fail_models = {PRIMARY_MODEL, BACKUP_MODEL}
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "route.model_unavailable"

    traces = client.get("/v1/traces").json()
    assert traces[0]["status"] == "failed"
    assert traces[0]["error_code"] == "route.model_unavailable"
    assert traces[0]["endpoint"] == "chat_completions"


def test_fallback_records_actual_target_in_trace(client, fake_provider):
    from tests.conftest import BACKUP_KEY

    fake_provider.fail_models = {PRIMARY_MODEL}
    fake_provider.contents = {BACKUP_MODEL: "from backup"}
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    assert response.json()["actual_model"] == BACKUP_KEY
    trace = client.get("/v1/traces").json()[0]
    assert trace["status"] == "success"
    assert trace["actual_model"] == BACKUP_KEY
    assert trace["cost_usd"] > 0


def test_stream_retries_target_per_config_before_fallback(client, fake_provider):
    # 流式与非流式一致：首块前同目标按 retries_per_target（默认 2）重试（初次+2 次=3 次尝试，ADR 0014），再切换候选
    fake_provider.fail_models = {PRIMARY_MODEL}
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert response.status_code == 200
    assert "[DONE]" in response.text  # 切换后流完整走完（正文被 SSE JSON 转义，不做原文断言）
    assert fake_provider.stream_calls.count(PRIMARY_MODEL) == 3
    assert fake_provider.stream_calls[-1] == BACKUP_MODEL


def test_legacy_llm_endpoints_removed(client):
    assert client.post("/v1/llm", json={"model": "fast", "messages": []}).status_code == 404
    assert client.post("/v1/llm/stream", json={"model": "fast", "messages": []}).status_code == 404

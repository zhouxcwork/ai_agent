from __future__ import annotations

from tests.conftest import PRIMARY_MODEL, BACKUP_MODEL


def test_all_targets_unavailable_records_failed_trace(client, fake_provider):
    fake_provider.fail_models = {PRIMARY_MODEL, BACKUP_MODEL}
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "model_unavailable"

    traces = client.get("/v1/traces").json()
    assert traces[0]["status"] == "failed"
    assert traces[0]["error_code"] == "model_unavailable"
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


def test_legacy_llm_endpoints_removed(client):
    assert client.post("/v1/llm", json={"model": "fast", "messages": []}).status_code == 404
    assert client.post("/v1/llm/stream", json={"model": "fast", "messages": []}).status_code == 404

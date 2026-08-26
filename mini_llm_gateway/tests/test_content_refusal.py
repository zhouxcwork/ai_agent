from __future__ import annotations

from tests.conftest import PRIMARY_MODEL


def test_refusal_non_stream_transparent_no_fallback(client, fake_provider):
    # 拒答透传（ADR 0010）：200 + finish_reason=content_filter；不重试、不换候选
    fake_provider.refuse_models = {PRIMARY_MODEL}
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["finish_reason"] == "content_filter"
    assert fake_provider.complete_calls == [PRIMARY_MODEL]  # 未 fallback 到 backup

    trace = client.get("/v1/traces").json()[0]
    assert trace["status"] == "refused"
    assert trace["error_code"] == "content.refused"


def test_refusal_stream_finish_reason_transparent(client, fake_provider):
    fake_provider.refuse_models = {PRIMARY_MODEL}
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert response.status_code == 200
    assert "content_filter" in response.text  # 尾块 finish_reason 透传
    assert "[DONE]" in response.text  # 正常收流而非流内错误

    trace = client.get("/v1/traces").json()[0]
    assert trace["status"] == "refused"


def test_refusal_responses_dialect_incomplete(client, fake_provider):
    # Responses 方言：拒答以 status=incomplete + incomplete_details 表达
    fake_provider.refuse_models = {PRIMARY_MODEL}
    response = client.post("/v1/responses", json={"model": "fast", "input": "hi"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "incomplete"
    assert body["incomplete_details"] == {"reason": "content_filter"}

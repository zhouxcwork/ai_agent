from __future__ import annotations

from mini_llm_gateway.schemas.llm import Usage
from tests.conftest import PRIMARY_MODEL


def test_trace_records_ttft_finish_reason_and_upstream_id(client, fake_provider):
    # 流式成功：TTFT（首个业务 delta）、finish_reason、供应商请求 ID 全部入库
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert response.status_code == 200
    trace = client.get("/v1/traces").json()[0]
    assert trace["ttft_ms"] is not None and trace["ttft_ms"] >= 0
    assert trace["ttft_ms"] <= trace["latency_ms"]
    assert trace["finish_reason"] == "stop"
    assert trace["upstream_request_id"] == f"upstream-{PRIMARY_MODEL}"


def test_trace_ttft_null_for_non_stream(client, fake_provider):
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    trace = client.get("/v1/traces").json()[0]
    assert trace["ttft_ms"] is None  # TTFT 只对流式有意义（ADR 0012）
    assert trace["upstream_request_id"] == f"upstream-{PRIMARY_MODEL}"


def test_run_id_header_recorded(client, fake_provider):
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Run-Id": "run-abc-123"},
    )
    assert response.status_code == 200
    assert client.get("/v1/traces").json()[0]["run_id"] == "run-abc-123"


def test_usage_breakdown_recorded(client, fake_provider):
    # cached/reasoning tokens 细分入库（cached 已含在 input_tokens 内）
    fake_provider.usage = Usage(input_tokens=100, output_tokens=50, cached_tokens=40, reasoning_tokens=7)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    trace = client.get("/v1/traces").json()[0]
    assert trace["input_tokens"] == 100
    assert trace["cached_tokens"] == 40
    assert trace["reasoning_tokens"] == 7


def test_refused_trace_carries_finish_reason(client, fake_provider):
    fake_provider.refuse_models = {PRIMARY_MODEL}
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert response.status_code == 200
    trace = client.get("/v1/traces").json()[0]
    assert trace["status"] == "refused"
    assert trace["finish_reason"] == "content_filter"

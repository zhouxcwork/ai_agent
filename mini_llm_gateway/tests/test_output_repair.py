from __future__ import annotations

import pytest
from openai import InternalServerError

from mini_llm_gateway.service.repair import extract_json
from tests.conftest import ADMIN_TOKEN_ENV, PRIMARY_KEY, PRIMARY_MODEL, make_config


SCHEMA = {"type": "object", "properties": {"a": {"type": "number"}}}


# ---- 提取修复纯函数 ----


def test_extract_markdown_code_block():
    assert extract_json('前缀\n```json\n{"a": 1}\n```\n后缀') == '{"a": 1}'


def test_extract_bare_code_block():
    assert extract_json('```\n{"a": 1}\n```') == '{"a": 1}'


def test_extract_surrounding_noise():
    assert extract_json('好的，结果如下：\n\n{"a": 1}\n\n希望有帮助') == '{"a": 1}'


def test_extract_plain_json_unchanged():
    assert extract_json('  {"a": 1}  ') == '{"a": 1}'


def test_extract_unrecoverable_passthrough():
    assert extract_json("完全不是 JSON") == "完全不是 JSON"


# ---- SDK 行为 ----


async def test_markdown_wrapped_json_repaired_silently(sdk, fake_provider):
    fake_provider.contents = {PRIMARY_MODEL: '```json\n{"a": 42}\n```'}
    completion = await sdk.chat.completions.create(
        model="fast",
        messages=[{"role": "user", "content": "give json"}],
        response_format={"type": "json_schema", "json_schema": {"name": "d", "schema": SCHEMA}},
    )
    assert completion.choices[0].message.content == '{"a": 42}'
    assert fake_provider.complete_calls == [PRIMARY_MODEL]  # 提取修复不触发上游重试


async def test_bad_json_retries_upstream_then_fails(sdk, fake_provider, client):
    fake_provider.contents = {PRIMARY_MODEL: "这不是 JSON"}
    with pytest.raises(InternalServerError) as exc_info:
        await sdk.chat.completions.create(
            model="fast",
            messages=[{"role": "user", "content": "hi"}],
            response_format={"type": "json_object"},
        )
    assert exc_info.value.response.json()["error"]["code"] == "invalid_json"
    # 1 次原始调用 + max_retries(2) 次重试 = 3 次，边界明确
    assert len(fake_provider.complete_calls) == 3


async def test_max_retries_configurable(tmp_path, fake_provider, monkeypatch):
    from fastapi.testclient import TestClient
    from mini_llm_gateway.app import create_app

    monkeypatch.setenv(ADMIN_TOKEN_ENV, "secret-token")
    app = create_app(
        make_config(str(tmp_path / "gw.db"), structured_max_retries=0), provider=fake_provider
    )
    with TestClient(app) as client:
        fake_provider.contents = {PRIMARY_MODEL: "bad"}
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "fast",
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": {"type": "json_object"},
            },
        )
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "invalid_json"
        assert len(fake_provider.complete_calls) == 1  # 0 重试：只有提取修复，不重试上游


async def test_content_failures_do_not_trip_circuit(sdk, fake_provider, client):
    # 反复内容失败不计熔断：目标保持 closed/healthy，流量不切走
    fake_provider.contents = {PRIMARY_MODEL: "坏输出"}
    for _ in range(4):
        with pytest.raises(InternalServerError):
            await sdk.chat.completions.create(
                model="fast",
                messages=[{"role": "user", "content": "hi"}],
                response_format={"type": "json_object"},
            )
    routes = {r["target"]: r for r in client.get("/v1/models").json()["gateway_routes"]["fast"]}
    assert routes[PRIMARY_KEY]["state"] == "closed"
    assert routes[PRIMARY_KEY]["failures"] == 0

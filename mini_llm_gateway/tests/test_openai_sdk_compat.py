from __future__ import annotations

import pytest
import openai
from openai import BadRequestError

from tests.conftest import BACKUP_MODEL, PRIMARY_KEY, PRIMARY_MODEL

async def test_sdk_chat_completion_roundtrip(sdk, fake_provider):
    fake_provider.contents = {PRIMARY_MODEL: "你好，世界"}
    completion = await sdk.chat.completions.create(
        model="fast", messages=[{"role": "user", "content": "hi"}]
    )
    assert completion.object == "chat.completion"
    assert completion.choices[0].message.content == "你好，世界"
    assert completion.choices[0].finish_reason == "stop"
    assert completion.model == "fast"  # requested_model（档位名）
    assert "actual_model" not in completion.model_dump()  # 路由目标不对外透出（ADR 0015）
    assert completion.usage.total_tokens == 15
    assert completion.id.startswith("chatcmpl-")


async def test_sdk_extra_fields_ignored(sdk, fake_provider):
    # SDK 常规携带采样参数，网关接受但忽略，不影响调用
    fake_provider.contents = {PRIMARY_MODEL: "ok"}
    completion = await sdk.chat.completions.create(
        model="fast",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
        top_p=0.9,
        max_tokens=100,
    )
    assert completion.choices[0].message.content == "ok"


async def test_sdk_gateway_prompt_via_extra_body(sdk, fake_provider, client):
    fake_provider.contents = {PRIMARY_MODEL: "ok"}
    created = client.post(
        "/v1/prompts",
        json={"name": "kb", "version": "v1", "system_template": "你是{{product}}的决策器"},
        headers={"X-Admin-Token": "secret-token"},
    )
    assert created.status_code == 201

    completion = await sdk.chat.completions.create(
        model="fast",
        messages=[{"role": "user", "content": "hi"}],
        extra_body={"gateway_prompt": {"name": "kb", "version": "v1", "variables": {"product": "知识库"}}},
    )
    assert completion.choices[0].message.content == "ok"
    trace = client.get("/v1/traces").json()[0]
    assert trace["endpoint"] == "chat_completions"
    assert trace["prompt_name"] == "kb"
    assert trace["requested_model"] == "fast"
    assert trace["actual_model"] == PRIMARY_KEY


async def test_sdk_structured_output_json_schema(sdk, fake_provider):
    fake_provider.contents = {PRIMARY_MODEL: '{"a": 42}'}
    completion = await sdk.chat.completions.create(
        model="fast",
        messages=[{"role": "user", "content": "give me json"}],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "demo",
                "schema": {"type": "object", "properties": {"a": {"type": "number"}}},
            },
        },
    )
    assert completion.choices[0].message.content == '{"a": 42}'
    trace_completion = completion  # SDK 不解析 content；结构化校验已在网关完成
    assert trace_completion.choices[0].finish_reason == "stop"


async def test_sdk_error_unknown_mode_raises_api_error(sdk):
    with pytest.raises(BadRequestError) as exc_info:
        await sdk.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    body = exc_info.value.response.json()
    assert body["error"]["code"] == "route.unknown_mode"
    assert body["error"]["type"] == "invalid_request_error"


async def test_sdk_error_tools_unsupported(sdk):
    with pytest.raises(BadRequestError) as exc_info:
        await sdk.chat.completions.create(
            model="fast",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
        )
    assert exc_info.value.response.json()["error"]["code"] == "request.unsupported_parameter"


async def test_sdk_error_validation_is_openai_format(client):
    response = client.post("/v1/chat/completions", json={"model": "fast"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "request.validation_error"


async def test_sdk_error_invalid_json_content(client, fake_provider):
    fake_provider.contents = {PRIMARY_MODEL: "这不是 JSON"}
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "fast",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_object"},
        },
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "output.invalid_json"


async def test_sdk_chat_stream_chunks(sdk, fake_provider):
    stream = await sdk.chat.completions.create(
        model="fast", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    chunks = [chunk async for chunk in stream]
    assert chunks[0].object == "chat.completion.chunk"
    assert chunks[0].choices[0].delta.role == "assistant"
    text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    assert text == "你好"
    assert [c for c in chunks if c.choices][-1].choices[0].finish_reason == "stop"
    assert {c.id for c in chunks} == {chunks[0].id}  # 全流 id 一致
    assert chunks[0].model == "fast"
    assert "actual_model" not in chunks[0].model_dump()


async def test_sdk_chat_stream_fallback_before_first_chunk(sdk, fake_provider):
    fake_provider.fail_models = {PRIMARY_MODEL}
    stream = await sdk.chat.completions.create(
        model="fast", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    chunks = [chunk async for chunk in stream]
    text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    assert text == "你好"  # 首块前切换目标，调用方拿到完整文本；切换事实只记 Trace


async def test_sdk_chat_stream_all_failed(sdk, fake_provider):
    fake_provider.fail_models = {PRIMARY_MODEL, BACKUP_MODEL}
    stream = await sdk.chat.completions.create(
        model="fast", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    # 无正文块；流内 error 事件由 SDK 转为 APIError 抛出
    with pytest.raises(openai.APIError):
        async for _chunk in stream:
            pass


async def test_sdk_chat_stream_structured_now_allowed(sdk, fake_provider):
    # 流式与结构化已解禁（ADR 0008）：不再 400
    fake_provider.stream_chunks = ['{"a": ', "42}"]
    stream = await sdk.chat.completions.create(
        model="fast",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        response_format={"type": "json_object"},
    )
    chunks = [c async for c in stream]
    assert [c for c in chunks if c.choices][-1].choices[0].finish_reason == "stop"

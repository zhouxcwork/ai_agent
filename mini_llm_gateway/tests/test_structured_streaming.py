from __future__ import annotations

import json

import openai
import pytest

from tests.conftest import PRIMARY_KEY, PRIMARY_MODEL


SCHEMA = {"type": "object", "properties": {"a": {"type": "number"}}}


async def test_chat_structured_stream_success(sdk, fake_provider, client):
    fake_provider.stream_chunks = ['{"a": ', "42}"]
    stream = await sdk.chat.completions.create(
        model="fast",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        response_format={"type": "json_schema", "json_schema": {"name": "d", "schema": SCHEMA}},
    )
    chunks = [c async for c in stream]
    text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    assert json.loads(text) == {"a": 42}
    assert [c for c in chunks if c.choices][-1].choices[0].finish_reason == "stop"

    trace = client.get("/v1/traces").json()[0]
    assert trace["status"] == "success"


async def test_chat_structured_stream_syntax_break_cuts_stream(sdk, fake_provider, client):
    fake_provider.stream_chunks = ['{"a": ', "[1}]"]  # 第二段括号失配
    stream = await sdk.chat.completions.create(
        model="fast",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        response_format={"type": "json_object"},
    )
    collected = []
    with pytest.raises(openai.APIError):  # 流内 error 事件由 SDK 转为异常
        async for chunk in stream:
            collected.append(chunk.choices[0].delta.content or "")
    assert collected == ['{"a": ']  # 破损增量到达前截断

    trace = client.get("/v1/traces").json()[0]
    assert trace["status"] == "failed"
    assert trace["error_code"] == "output.invalid_json"


async def test_chat_structured_stream_schema_mismatch_fails_at_tail(sdk, fake_provider, client):
    fake_provider.stream_chunks = ['{"a": ', '"str"}']  # 语法完好但 a 非 number
    stream = await sdk.chat.completions.create(
        model="fast",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        response_format={"type": "json_schema", "json_schema": {"name": "d", "schema": SCHEMA}},
    )
    with pytest.raises(openai.APIError):
        async for _chunk in stream:
            pass

    trace = client.get("/v1/traces").json()[0]
    assert trace["status"] == "failed"
    assert trace["error_code"] == "output.schema_validation_failed"


async def test_responses_structured_stream_success(sdk, fake_provider):
    fake_provider.stream_chunks = ['{"a": ', "42}"]
    events = [
        e
        async for e in await sdk.responses.create(
            model="fast",
            input="hi",
            stream=True,
            text={"format": {"type": "json_schema", "name": "d", "schema": SCHEMA}},
        )
    ]
    assert events[-1].type == "response.completed"
    assert json.loads(events[-1].response.output_text) == {"a": 42}


async def test_responses_structured_stream_syntax_break(sdk, fake_provider, client):
    fake_provider.stream_chunks = ['{"a": ', "[1}]"]
    events = [
        e
        async for e in await sdk.responses.create(
            model="fast", input="hi", stream=True, text={"format": {"type": "json_object"}}
        )
    ]
    failed = [e for e in events if e.type == "response.failed"]
    assert failed and failed[0].response.error.code == "output.invalid_json"

    trace = client.get("/v1/traces").json()[0]
    assert trace["error_code"] == "output.invalid_json"


async def test_structured_content_failures_do_not_trip_circuit(sdk, fake_provider, client):
    # 内容类失败不计熔断（ADR 0009）：反复语法破损后目标仍 healthy
    fake_provider.stream_chunks = ['{"a": ', "[1}]"]
    for _ in range(4):
        stream = await sdk.chat.completions.create(
            model="fast",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            response_format={"type": "json_object"},
        )
        with pytest.raises(openai.APIError):
            async for _c in stream:
                pass

    routes = {r["target"]: r for r in client.get("/v1/models").json()["gateway_routes"]["fast"]}
    assert routes[PRIMARY_KEY]["state"] == "closed"
    assert routes[PRIMARY_KEY]["healthy"] is True

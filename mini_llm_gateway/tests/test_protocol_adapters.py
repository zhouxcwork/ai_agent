from __future__ import annotations

from types import SimpleNamespace

import pytest

from mini_llm_gateway.config import ResolvedTarget
from mini_llm_gateway.provider.anthropic_adapter import (
    AnthropicAdapter,
    _map_stop_reason,
    _translate_messages,
)
from mini_llm_gateway.provider.base import ContentRefusedError, FinishReason, UpstreamID
from mini_llm_gateway.provider.responses_adapter import ResponsesAdapter
from mini_llm_gateway.schemas.llm import Message, Usage


def anthropic_target() -> ResolvedTarget:
    return ResolvedTarget(
        provider="deepseek-anthropic",
        provider_model="deepseek-v4-flash",
        key="deepseek-anthropic/deepseek-v4-flash",
        protocol="anthropic",
        base_url="https://api.deepseek.com/anthropic",
        api_key_env="DEEPSEEK_API_KEY",
        supports_structured_output=True,
        structured_output_mode="tool_use",
        price_per_million={"input": 0.42, "output": 1.27},
    )


def responses_target(mode: str = "json_schema") -> ResolvedTarget:
    return ResolvedTarget(
        provider="deepseek-responses",
        provider_model="deepseek-v4-pro",
        key="deepseek-responses/deepseek-v4-pro",
        protocol="responses",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        supports_structured_output=True,
        structured_output_mode=mode,
        price_per_million={"input": 1.27, "output": 3.8},
    )


MSGS = [
    Message(role="system", content="你是助手"),
    Message(role="user", content="你好"),
]


# ---------- Anthropic：消息与请求构造 ----------


def test_anthropic_system_extracted_to_top_level():
    system, conversation = _translate_messages(MSGS)
    assert system == "你是助手"
    assert conversation == [{"role": "user", "content": "你好"}]


def test_anthropic_structured_uses_forced_tool():
    schema = {"type": "object", "properties": {"score": {"type": "number"}}}
    request = AnthropicAdapter._build_request(anthropic_target(), MSGS, 30, schema)
    assert request["tools"][0]["input_schema"] == schema
    assert request["tool_choice"] == {"type": "tool", "name": "emit_json"}
    assert request["system"] == "你是助手"


def test_anthropic_plain_request_has_no_tools():
    request = AnthropicAdapter._build_request(anthropic_target(), MSGS, 30, None)
    assert "tools" not in request and "tool_choice" not in request
    assert request["max_tokens"] > 0  # Anthropic 协议必填


@pytest.mark.parametrize(
    "anthropic,expected",
    [("end_turn", "stop"), ("stop_sequence", "stop"), ("max_tokens", "length"), ("refusal", "content_filter")],
)
def test_anthropic_stop_reason_mapping(anthropic, expected):
    assert _map_stop_reason(anthropic) == expected


# ---------- Anthropic：complete / stream（mock client） ----------


def _fake_anthropic_client(response):
    # 假 client：messages.create 返回固定响应（UpstreamID/FinishReason 无 __eq__，断言比较 .value）
    async def create(**kwargs):
        return response

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def _anthropic_response(blocks, stop_reason="end_turn"):
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        id="msg_1",
        usage=SimpleNamespace(input_tokens=7, output_tokens=3, cache_read_input_tokens=2),
    )


async def test_anthropic_complete_extracts_tool_use_json(monkeypatch):
    response = _anthropic_response(
        [SimpleNamespace(type="text", text="前言"), SimpleNamespace(type="tool_use", input={"score": 9})]
    )
    adapter = AnthropicAdapter()
    monkeypatch.setattr(adapter, "create_client", lambda target: _fake_anthropic_client(response))
    content, usage, upstream_id = await adapter.complete(anthropic_target(), MSGS, 30, {"type": "object"})
    assert content == '{"score": 9}'  # tool_use.input 序列化回字符串，两层校验照常工作
    assert usage == Usage(input_tokens=7, output_tokens=3, cached_tokens=2, reasoning_tokens=0)
    assert upstream_id == "msg_1"


async def test_anthropic_complete_refusal_raises(monkeypatch):
    response = _anthropic_response([SimpleNamespace(type="text", text="")], stop_reason="refusal")
    adapter = AnthropicAdapter()
    monkeypatch.setattr(adapter, "create_client", lambda target: _fake_anthropic_client(response))
    with pytest.raises(ContentRefusedError):
        await adapter.complete(anthropic_target(), MSGS, 30, None)


async def _run_anthropic_stream(monkeypatch, events):
    async def gen():
        for event in events:
            yield event

    async def create(**kwargs):
        return gen()

    adapter = AnthropicAdapter()
    monkeypatch.setattr(adapter, "create_client", lambda target: SimpleNamespace(
        messages=SimpleNamespace(create=create)))
    return [item async for item in adapter.stream(anthropic_target(), MSGS, 30)]


async def test_anthropic_stream_translates_events(monkeypatch):
    events = [
        SimpleNamespace(type="message_start", message=SimpleNamespace(
            id="msg_2", usage=SimpleNamespace(input_tokens=10, output_tokens=1, cache_read_input_tokens=4))),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="你")),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="好")),
        SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason="end_turn"),
                        usage=SimpleNamespace(output_tokens=5)),
    ]
    signals = await _run_anthropic_stream(monkeypatch, events)
    assert signals[0].value == "msg_2"  # UpstreamID
    assert signals[1:3] == ["你", "好"]
    assert signals[3].value == "stop"  # FinishReason
    assert signals[4] == Usage(input_tokens=10, output_tokens=5, cached_tokens=4, reasoning_tokens=0)


async def test_anthropic_stream_tool_json_deltas(monkeypatch):
    # 结构化流式：input_json_delta 增量即业务增量（供语法监控与流尾拼接，ADR 0008/0013）
    events = [
        SimpleNamespace(type="message_start", message=SimpleNamespace(
            id="msg_3", usage=SimpleNamespace(input_tokens=8, output_tokens=0, cache_read_input_tokens=0))),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="input_json_delta", partial_json='{"a"')),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="input_json_delta", partial_json=": 1}")),
        SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason="tool_use"),
                        usage=SimpleNamespace(output_tokens=6)),
    ]
    signals = await _run_anthropic_stream(monkeypatch, events)
    assert signals[1:3] == ['{"a"', ": 1}"]
    assert signals[3].value == "stop"  # tool_use 结束映射为 stop


# ---------- Responses：请求构造与流事件 ----------


def test_responses_build_request_instructions_and_schema():
    schema = {"type": "object"}
    request = ResponsesAdapter._build_request(responses_target(), MSGS, 30, schema)
    assert request["instructions"] == "你是助手"
    assert request["input"] == [{"role": "user", "content": "你好"}]
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["schema"] == schema


def test_responses_json_object_mode_injects_schema_into_instructions():
    request = ResponsesAdapter._build_request(responses_target("json_object"), MSGS, 30, {"type": "object"})
    assert request["text"] == {"format": {"type": "json_object"}}
    assert "JSON Schema" in request["instructions"]


async def test_responses_stream_translates_events(monkeypatch):
    events = [
        SimpleNamespace(type="response.created", response=SimpleNamespace(id="resp_1")),
        SimpleNamespace(type="response.output_text.delta", delta="hi"),
        SimpleNamespace(type="response.completed", response=SimpleNamespace(
            id="resp_1",
            usage=SimpleNamespace(
                input_tokens=12, output_tokens=6,
                input_tokens_details=SimpleNamespace(cached_tokens=5),
                output_tokens_details=SimpleNamespace(reasoning_tokens=2),
            ),
        )),
    ]

    class StreamClient:
        class responses:
            @staticmethod
            async def create(**kwargs):
                async def gen():
                    for event in events:
                        yield event

                return gen()

    adapter = ResponsesAdapter()
    monkeypatch.setattr(adapter, "create_client", lambda target: StreamClient())
    signals = [item async for item in adapter.stream(responses_target(), MSGS, 30)]
    assert signals[0].value == "resp_1"  # UpstreamID
    assert signals[1] == "hi"
    assert signals[2].value == "stop"  # FinishReason
    assert signals[3] == Usage(input_tokens=12, output_tokens=6, cached_tokens=5, reasoning_tokens=2)

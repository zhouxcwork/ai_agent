from __future__ import annotations

import pytest
from openai import BadRequestError, NotFoundError

from tests.conftest import BACKUP_MODEL, PRIMARY_KEY, PRIMARY_MODEL



async def test_sdk_responses_create(sdk, fake_provider):
    fake_provider.contents = {PRIMARY_MODEL: "这是回答"}
    response = await sdk.responses.create(model="fast", input="你好")
    assert response.object == "response"
    assert response.status == "completed"
    assert response.output_text == "这是回答"
    assert response.model == "fast"
    assert response.actual_model == PRIMARY_KEY
    assert response.id.startswith("resp-")
    assert response.usage.total_tokens == 15


async def test_sdk_responses_instructions_as_system(sdk, fake_provider):
    fake_provider.contents = {PRIMARY_MODEL: "ok"}
    await sdk.responses.create(model="fast", input="hi", instructions="你是一个翻译器")
    assert fake_provider.last_messages[0].role == "system"
    assert fake_provider.last_messages[0].content == "你是一个翻译器"
    assert fake_provider.last_messages[1].role == "user"


async def test_sdk_responses_input_message_array_and_blocks(sdk, fake_provider, client):
    fake_provider.contents = {PRIMARY_MODEL: "ok"}
    response = await sdk.responses.create(
        model="fast",
        input=[
            {"role": "user", "content": [{"type": "input_text", "text": "第一段"}, {"type": "input_text", "text": "第二段"}]},
        ],
    )
    assert response.status == "completed"


async def test_sdk_responses_structured_text_format(sdk, fake_provider):
    fake_provider.contents = {PRIMARY_MODEL: '{"a": 1}'}
    response = await sdk.responses.create(
        model="fast",
        input="give me json",
        text={"format": {"type": "json_schema", "name": "demo", "schema": {"type": "object", "properties": {"a": {"type": "integer"}}}}},
    )
    assert response.output_text == '{"a": 1}'


async def test_sdk_responses_conversation_chain(sdk, fake_provider):
    # 三轮链式对话：上下文逐轮累积（父输入 + 父输出 + 新输入）
    from tests.conftest import BACKUP_MODEL

    fake_provider.contents = {PRIMARY_MODEL: "ack", BACKUP_MODEL: "ack"}

    first = await sdk.responses.create(model="fast", input="第一轮")
    assert [m.content for m in fake_provider.last_messages] == ["第一轮"]

    second = await sdk.responses.create(model="fast", input="第二轮", previous_response_id=first.id)
    assert [m.content for m in fake_provider.last_messages] == ["第一轮", "ack", "第二轮"]

    third = await sdk.responses.create(
        model="fast", input="第三轮", previous_response_id=second.id
    )
    assert [m.content for m in fake_provider.last_messages] == [
        "第一轮", "ack", "第二轮", "ack", "第三轮",
    ]
    assert third.status == "completed"


async def test_sdk_responses_store_false_not_referenceable(sdk, fake_provider):
    fake_provider.contents = {PRIMARY_MODEL: "one-shot"}
    response = await sdk.responses.create(model="fast", input="hi", store=False)
    assert response.status == "completed"
    with pytest.raises(NotFoundError):
        await sdk.responses.create(model="fast", input="again", previous_response_id=response.id)


async def test_sdk_responses_unknown_previous_id_404(sdk):
    with pytest.raises(NotFoundError):
        await sdk.responses.create(model="fast", input="hi", previous_response_id="resp-nope")


async def test_sdk_responses_rejects_tools(sdk):
    with pytest.raises(BadRequestError) as exc_info:
        await sdk.responses.create(
            model="fast",
            input="hi",
            tools=[{"type": "function", "name": "f", "parameters": {}, "description": ""}],
        )
    assert exc_info.value.response.json()["error"]["code"] == "request.unsupported_parameter"


async def test_responses_trace_endpoint(client, fake_provider):
    fake_provider.contents = {PRIMARY_MODEL: "ok"}
    client.post("/v1/responses", json={"model": "fast", "input": "hi"})
    trace = client.get("/v1/traces").json()[0]
    assert trace["endpoint"] == "responses"
    assert trace["requested_model"] == "fast"
    assert trace["actual_model"] == PRIMARY_KEY


async def test_sdk_responses_stream_events(sdk, fake_provider):
    fake_provider.contents = {PRIMARY_MODEL: "ignored"}
    stream = await sdk.responses.create(model="fast", input="hi", stream=True)
    events = [event async for event in stream]
    types = [e.type for e in events]
    assert types[0] == "response.created"
    assert events[0].response.status == "in_progress"
    deltas = [e.delta for e in events if e.type == "response.output_text.delta"]
    assert "".join(deltas) == "你好"
    assert types[-1] == "response.completed"
    completed = events[-1].response
    assert completed.status == "completed"
    assert completed.output_text == "你好"
    assert completed.model == "fast"
    assert completed.actual_model == PRIMARY_KEY


async def test_sdk_responses_stream_failed(sdk, fake_provider):
    from tests.conftest import BACKUP_MODEL

    fake_provider.fail_models = {PRIMARY_MODEL, BACKUP_MODEL}
    stream = await sdk.responses.create(model="fast", input="hi", stream=True)
    events = [event async for event in stream]
    types = [e.type for e in events]
    assert types[0] == "response.created"  # 失败流也以 created 开头
    failed = [e for e in events if e.type == "response.failed"]
    assert failed and failed[0].response.status == "failed"


async def test_sdk_responses_stream_stored_for_continuation(sdk, fake_provider):
    stream = await sdk.responses.create(model="fast", input="第一轮", stream=True)
    events = [event async for event in stream]
    first_id = events[-1].response.id
    # 流式响应同样入库（累积文本），可被续接
    fake_provider.contents = {PRIMARY_MODEL: "ack", BACKUP_MODEL: "ack"}
    second = await sdk.responses.create(model="fast", input="第二轮", previous_response_id=first_id)
    assert second.status == "completed"
    assert [m.content for m in fake_provider.last_messages] == ["第一轮", "你好", "第二轮"]

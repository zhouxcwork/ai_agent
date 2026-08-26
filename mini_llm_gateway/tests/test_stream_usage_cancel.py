from __future__ import annotations

import pytest

from mini_llm_gateway.config import GatewayConfig
from mini_llm_gateway.repository.prompt_repository import PromptRepository
from mini_llm_gateway.repository.trace_repository import TraceRepository
from mini_llm_gateway.schemas.llm import LLMRequest, Message
from mini_llm_gateway.service.gateway_service import GatewayService
from mini_llm_gateway.service.model_router import ModelRouter
from tests.conftest import PRIMARY_KEY, PRIMARY_MODEL, make_config

pytestmark = pytest.mark.asyncio


async def test_chat_stream_usage_chunk_and_trace(sdk, fake_provider, client):
    stream = await sdk.chat.completions.create(
        model="fast", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    chunks = [c async for c in stream]
    usage_chunks = [c for c in chunks if c.usage is not None and c.usage.total_tokens > 0]
    assert usage_chunks, "应有携带真实 usage 的末尾 chunk"
    assert usage_chunks[0].usage.total_tokens == 15

    trace = client.get("/v1/traces").json()[0]
    assert trace["input_tokens"] == 10
    assert trace["output_tokens"] == 5
    assert trace["cost_usd"] > 0  # 真实成本，不再是 0


async def test_responses_stream_completed_carries_usage(sdk, fake_provider):
    events = [e async for e in await sdk.responses.create(model="fast", input="hi", stream=True)]
    completed = events[-1]
    assert completed.type == "response.completed"
    assert completed.response.usage.input_tokens == 10
    assert completed.response.usage.output_tokens == 5
    assert completed.response.usage.total_tokens == 15


async def test_client_disconnect_records_cancelled_trace(tmp_path, fake_provider):
    config: GatewayConfig = make_config(str(tmp_path / "gw.db"))
    prompts = PromptRepository(config.database.path)
    traces = TraceRepository(config.database.path)
    await prompts.initialize()
    await traces.initialize()
    gateway = GatewayService(config, ModelRouter(config), fake_provider, prompts, traces)

    request = LLMRequest(model="fast", messages=[Message(role="user", content="hi")], stream=True)
    generator = gateway.stream(request)
    first = await generator.__anext__()
    assert first["type"] == "content.delta"
    await generator.aclose()  # 模拟客户端断连（GeneratorExit 路径）

    rows = await traces.list()
    assert rows[0].status == "cancelled"
    assert rows[0].error_code == "client_cancelled"
    assert rows[0].actual_model == PRIMARY_KEY
    assert rows[0].input_tokens == 0  # usage 末块尚未到达


async def test_completed_stream_not_marked_cancelled(sdk, client):
    # 正常完成的流不受取消处理影响
    stream = await sdk.chat.completions.create(
        model="fast", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    chunks = [c async for c in stream]
    assert [c for c in chunks if c.choices][-1].choices[0].finish_reason == "stop"
    trace = client.get("/v1/traces").json()[0]
    assert trace["status"] == "success"

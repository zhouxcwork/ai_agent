from __future__ import annotations

from mini_llm_gateway.config import ResolvedTarget
from mini_llm_gateway.schemas.llm import Usage
from mini_llm_gateway.service.gateway_service import GatewayService
from tests.conftest import PRIMARY_MODEL


def _target(price: dict[str, float]) -> ResolvedTarget:
    return ResolvedTarget(
        provider="p1",
        provider_model=PRIMARY_MODEL,
        key=f"p1/{PRIMARY_MODEL}",
        base_url="http://p1.test",
        api_key_env="K1",
        supports_structured_output=True,
        structured_output_mode="json_object",
        price_per_million=price,
    )


def _service() -> GatewayService:
    return GatewayService.__new__(GatewayService)  # 只测纯函数 calculate_cost，不初始化依赖


def test_cost_without_cached_price_key_falls_back_to_input():
    service = _service()
    target = _target({"input": 1.0, "output": 4.0})
    # 无 cached 键：缓存部分按输入全价，与旧公式一致
    usage = Usage(input_tokens=100, output_tokens=50, cached_tokens=40)
    assert service.calculate_cost(target, usage) == (60 * 1.0 + 40 * 1.0 + 50 * 4.0) / 1_000_000


def test_cost_with_cached_discount():
    service = _service()
    target = _target({"input": 1.0, "cached": 0.1, "output": 4.0})
    usage = Usage(input_tokens=100, output_tokens=50, cached_tokens=40)
    assert service.calculate_cost(target, usage) == (60 * 1.0 + 40 * 0.1 + 50 * 4.0) / 1_000_000


def test_cost_caps_cached_at_input():
    # 防御：上游异常返回 cached > input 时按 input 截断
    service = _service()
    target = _target({"input": 1.0, "cached": 0.1, "output": 4.0})
    usage = Usage(input_tokens=10, output_tokens=0, cached_tokens=99)
    assert service.calculate_cost(target, usage) == (0 * 1.0 + 10 * 0.1) / 1_000_000

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mini_llm_gateway.config import (
    CircuitBreakerConfig,
    GatewayConfig,
    ModeConfig,
    ProviderConfig,
    RouteTarget,
)
from mini_llm_gateway.errors import GatewayError
from mini_llm_gateway.service.model_router import ModelRouter


def make_config(fast_targets: list[RouteTarget], threshold: int = 5, cooldown: float = 30.0) -> GatewayConfig:
    return GatewayConfig(
        circuit_breaker=CircuitBreakerConfig(failure_threshold=threshold, cooldown_seconds=cooldown),
        providers={
            "p1": ProviderConfig(base_url="http://p1", api_key_env="K1"),
            "p2": ProviderConfig(base_url="http://p2", api_key_env="K2"),
            "disabled": ProviderConfig(base_url="http://d", api_key_env="KD", enabled=False),
        },
        modes={"fast": ModeConfig(targets=fast_targets)},
    )


def target(provider: str, model: str, weight: int = 1, structured: bool = True) -> RouteTarget:
    return RouteTarget(
        provider=provider,
        model=model,
        weight=weight,
        supports_structured_output=structured,
        structured_output_mode="json_object",
        price_per_million={"input": 1.0, "output": 4.0},
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def test_unknown_mode_raises(clock):
    router = ModelRouter(make_config([target("p1", "m1")]), clock=clock)
    with pytest.raises(GatewayError) as exc_info:
        router.candidates("slow")
    assert exc_info.value.code == "unknown_mode"
    assert exc_info.value.status_code == 400


def test_invalid_config_rejected():
    with pytest.raises(ValidationError):
        make_config([RouteTarget(provider="no-such-provider", model="m1")])  # 引用未定义供应商


def test_disabled_provider_filtered(clock):
    router = ModelRouter(make_config([target("disabled", "m1"), target("p1", "m2")]), clock=clock)
    keys = [t.key for t in router.candidates("fast")]
    assert keys == ["p1/m2"]


def test_structured_output_filters_targets(clock):
    config = make_config([target("p1", "plain", structured=False), target("p2", "smart-one")])
    router = ModelRouter(config, clock=clock)
    keys = [t.key for t in router.candidates("fast", structured=True)]
    assert keys == ["p2/smart-one"]


def test_no_healthy_route(clock):
    router = ModelRouter(make_config([target("disabled", "m1")]), clock=clock)
    with pytest.raises(GatewayError) as exc_info:
        router.candidates("fast")
    assert exc_info.value.code == "no_healthy_route"
    assert exc_info.value.status_code == 503


def test_weighted_round_robin_distribution(clock):
    router = ModelRouter(
        make_config([target("p1", "heavy", weight=3), target("p2", "light", weight=1)]), clock=clock
    )
    primaries = [router.candidates("fast")[0].key for _ in range(400)]
    assert primaries.count("p1/heavy") == 300
    assert primaries.count("p2/light") == 100


def test_fallback_order_after_primary(clock):
    router = ModelRouter(
        make_config([target("p1", "heavy", weight=3), target("p2", "light", weight=1)]), clock=clock
    )
    order = [t.key for t in router.candidates("fast")]
    assert order[0] in {"p1/heavy", "p2/light"}
    assert set(order) == {"p1/heavy", "p2/light"}


def test_circuit_opens_after_threshold_and_recovers_after_cooldown(clock):
    router = ModelRouter(
        make_config([target("p1", "flaky"), target("p2", "stable")], threshold=3, cooldown=30.0),
        clock=clock,
    )
    for _ in range(3):
        router.record_failure("p1/flaky")

    # 熔断中：p1 不再被选中
    assert [t.key for t in router.candidates("fast")] == ["p2/stable"]

    # 冷却期满 → 半开放行一次试探（目标重新可见）
    clock.now += 31
    assert "p1/flaky" in [t.key for t in router.candidates("fast")]

    # 试探失败 → 立即重新熔断
    router.record_failure("p1/flaky")
    assert [t.key for t in router.candidates("fast")] == ["p2/stable"]

    # 再冷却 → 试探成功 → 恢复
    clock.now += 31
    assert "p1/flaky" in [t.key for t in router.candidates("fast")]
    router.record_success("p1/flaky")
    assert len(router.candidates("fast")) == 2


def test_success_resets_failure_count(clock):
    router = ModelRouter(
        make_config([target("p1", "flaky"), target("p2", "stable")], threshold=3), clock=clock
    )
    router.record_failure("p1/flaky")
    router.record_failure("p1/flaky")
    router.record_success("p1/flaky")  # 清零连续失败
    router.record_failure("p1/flaky")
    assert len(router.candidates("fast")) == 2  # 未达阈值，仍在池中


def test_all_targets_tripped_raises_no_healthy_route(clock):
    router = ModelRouter(
        make_config([target("p1", "a"), target("p2", "b")], threshold=2), clock=clock
    )
    for key in ("p1/a", "p2/b"):
        router.record_failure(key)
        router.record_failure(key)
    with pytest.raises(GatewayError) as exc_info:
        router.candidates("fast")
    assert exc_info.value.code == "no_healthy_route"


def test_status_reports_health(clock):
    router = ModelRouter(
        make_config([target("p1", "a"), target("p2", "b")], threshold=2), clock=clock
    )
    router.record_failure("p1/a")
    status = router.status()
    assert status["p1/a"]["failures"] == 1
    assert status["p1/a"]["state"] == "closed"
    router.record_failure("p1/a")
    assert router.status()["p1/a"]["state"] == "open"

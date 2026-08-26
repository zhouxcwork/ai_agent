from __future__ import annotations

import itertools
import json
import logging
import time
from collections.abc import Callable

from mini_llm_gateway.config import GatewayConfig, ResolvedTarget, RouteTarget
from mini_llm_gateway.errors import GatewayError

logger = logging.getLogger("llm_gateway")

_HALF_OPEN = "half-open"
_CLOSED = "closed"
_OPEN = "open"


class _CircuitState:
    __slots__ = ("failures", "state", "opened_at")

    def __init__(self) -> None:
        self.failures = 0
        self.state: str = _CLOSED
        self.opened_at: float | None = None


class ModelRouter:
    # 档位路由 + 按路由目标粒度的熔断健康度。
    # 候选按权重展开轮询确定主选；主选之后按配置顺序排列其余健康候选做 fallback。
    # 熔断：连续失败达阈值 → open；冷却期满 → half-open 放行一次试探；
    # 试探成功 → closed，失败 → 立即 open。时钟可注入以便确定性测试。
    def __init__(self, config: GatewayConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.clock = clock
        self._circuits: dict[str, _CircuitState] = {}
        self._counters: dict[str, itertools.count] = {}

    def candidates(
        self, mode: str, *, structured: bool = False, request_id: str | None = None
    ) -> list[ResolvedTarget]:
        mode_config = self.config.modes.get(mode)
        if mode_config is None:
            raise GatewayError("route.unknown_mode", f"档位不存在: {mode}（合法值: {sorted(self.config.modes)}）", 400)
        healthy: list[RouteTarget] = []
        skipped: list[dict[str, str]] = []
        for target in mode_config.targets:
            if not self.config.providers[target.provider].enabled:
                skipped.append({"target": target.key, "reason": "provider_disabled"})
            elif not self._is_available(target.key):
                skipped.append({"target": target.key, "reason": "circuit_open"})
            elif structured and not target.supports_structured_output:
                skipped.append({"target": target.key, "reason": "capability_mismatch"})
            else:
                healthy.append(target)
        if not healthy:
            raise GatewayError("route.no_healthy_route", f"档位 {mode} 没有健康且能力匹配的路由目标", 503)
        ordered = self._ordered(mode, healthy)
        # 路由决策留痕：选中链（主选在前）+ 跳过项及原因，与 trace 的 attempts/actual_model 互补
        logger.info(
            "route_decision=%s",
            json.dumps(
                {
                    "request_id": request_id,
                    "mode": mode,
                    "structured": structured,
                    "selected": [t.key for t in ordered],
                    "skipped": skipped,
                },
                ensure_ascii=False,
            ),
        )
        return [self._resolve(target) for target in ordered]

    def _ordered(self, mode: str, healthy: list[RouteTarget]) -> list[RouteTarget]:
        # 权重展开轮询选主选；其余候选按原配置顺序跟随，作为 fallback 链。
        weighted = [target for target in healthy for _ in range(target.weight)]
        counter = self._counters.setdefault(mode, itertools.count())
        offset = next(counter) % len(weighted)
        primary = weighted[offset]
        return [primary, *[t for t in healthy if t is not primary]]

    def _resolve(self, target: RouteTarget) -> ResolvedTarget:
        provider = self.config.providers[target.provider]
        return ResolvedTarget(
            provider=target.provider,
            provider_model=target.model,
            key=target.key,
            base_url=provider.base_url,
            api_key_env=provider.api_key_env,
            supports_structured_output=target.supports_structured_output,
            structured_output_mode=target.structured_output_mode,
            price_per_million=target.price_per_million,
        )

    def record_success(self, target_key: str) -> None:
        self._circuits[target_key] = _CircuitState()

    def record_failure(self, target_key: str) -> None:
        state = self._circuits.setdefault(target_key, _CircuitState())
        state.failures += 1
        if state.state == _HALF_OPEN or state.failures >= self.config.circuit_breaker.failure_threshold:
            state.state = _OPEN
            state.opened_at = self.clock()

    def has_candidates(self, mode: str, *, structured: bool = False) -> bool:
        # 预检接口：与 candidates 相同的过滤条件，但不推进轮询计数器、
        # 供请求入口做快速失败校验，避免与真正选路重复消耗轮转位置。
        mode_config = self.config.modes.get(mode)
        if mode_config is None:
            raise GatewayError("route.unknown_mode", f"档位不存在: {mode}（合法值: {sorted(self.config.modes)}）", 400)
        return any(
            self._is_available(target.key)
            and self.config.providers[target.provider].enabled
            and (target.supports_structured_output or not structured)
            for target in mode_config.targets
        )

    def _is_available(self, target_key: str) -> bool:
        state = self._circuits.get(target_key)
        if state is None or state.state == _CLOSED:
            return True
        if state.state == _OPEN and state.opened_at is not None:
            if self.clock() - state.opened_at >= self.config.circuit_breaker.cooldown_seconds:
                state.state = _HALF_OPEN  # 进入放行窗口，等待试探结果
                return True
            return False
        return True  # half-open：放行窗口内允许调用，record_success/failure 决定去留

    def status(self) -> dict[str, dict[str, object]]:
        return {
            key: {"failures": state.failures, "state": self._display_state(key, state)}
            for key, state in self._circuits.items()
        }

    def _display_state(self, key: str, state: _CircuitState) -> str:
        if state.state == _OPEN:
            # 对外展示时，已过冷却期的 open 视为即将试探的 half-open
            if self.clock() - (state.opened_at or 0) >= self.config.circuit_breaker.cooldown_seconds:
                return _HALF_OPEN
            return _OPEN
        return state.state

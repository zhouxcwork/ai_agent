from __future__ import annotations

import time
from collections.abc import Callable

from mini_llm_gateway.config import GatewayConfig, LimitsConfig
from mini_llm_gateway.errors import GatewayError

_ANONYMOUS = "anonymous"


class _TokenBucket:
    # 单进程令牌桶：匀速补充、容量即突发额度，每次调用扣 1 token。
    # asyncio 单线程模型下无 await 点，无需加锁。
    __slots__ = ("rate", "capacity", "tokens", "updated_at")

    def __init__(self, rate: float, capacity: int, now: float) -> None:
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self.updated_at = now

    def try_take(self, now: float) -> bool:
        self.tokens = min(self.capacity, self.tokens + (now - self.updated_at) * self.rate)
        self.updated_at = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class _ConcurrencySlot:
    __slots__ = ("limit", "in_flight")

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.in_flight = 0

    def try_acquire(self) -> bool:
        if self.in_flight >= self.limit:
            return False
        self.in_flight += 1
        return True

    def release(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)


class Limiter:
    # 多级资源边界（ADR 0011，修订：并发按路由目标粒度）：租户 QPS 令牌桶 + 租户并发在
    # 请求入口快速拒绝；路由目标（供应商/模型）并发在候选尝试处占用，同供应商的不同
    # 模型互不挤占，对齐上游按模型的并发配额。时钟可注入以便确定性测试。
    def __init__(self, config: GatewayConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits: LimitsConfig = config.limits
        self.clock = clock
        self._buckets: dict[str, _TokenBucket] = {}
        self._tenant_slots: dict[str, _ConcurrencySlot] = {}
        self._target_slots = {
            key: _ConcurrencySlot(target_limit.max_concurrency)
            for key, target_limit in config.limits.targets.items()
        }

    def _tenant_limit(self, tenant: str):
        return self.limits.tenants.get(tenant, self.limits.default)

    def enter_tenant(self, tenant: str | None) -> None:
        # 请求入口资源预检：QPS 桶先扣减，再占并发槽；任一不足即 429 快速拒绝。
        tenant_id = tenant or _ANONYMOUS
        limit = self._tenant_limit(tenant_id)
        bucket = self._buckets.setdefault(
            tenant_id, _TokenBucket(limit.qps, limit.burst, self.clock())
        )
        if not bucket.try_take(self.clock()):
            raise GatewayError(
                "resource.tenant_rate_limited",
                f"租户 {tenant_id} 超过 QPS 限制（{limit.qps}/s，突发 {limit.burst}）",
                429,
            )
        slot = self._tenant_slots.setdefault(tenant_id, _ConcurrencySlot(limit.max_concurrency))
        if not slot.try_acquire():
            raise GatewayError(
                "resource.tenant_busy",
                f"租户 {tenant_id} 并发已满（{limit.max_concurrency}）",
                429,
            )

    def exit_tenant(self, tenant: str | None) -> None:
        slot = self._tenant_slots.get(tenant or _ANONYMOUS)
        if slot is not None:
            slot.release()

    def try_acquire_target(self, target_key: str) -> bool:
        slot = self._target_slots.get(target_key)
        return slot.try_acquire() if slot is not None else True

    def release_target(self, target_key: str) -> None:
        slot = self._target_slots.get(target_key)
        if slot is not None:
            slot.release()

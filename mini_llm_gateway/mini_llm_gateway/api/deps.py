from __future__ import annotations

import os
from collections.abc import AsyncIterator

from fastapi import Request

from mini_llm_gateway.errors import GatewayError
from mini_llm_gateway.service.limiter import Limiter


async def require_admin(request: Request) -> None:
    # 模板写接口的极简鉴权：静态令牌从环境变量注入，与调用方 API 隔离。
    expected = os.getenv(request.app.state.config.admin.token_env)
    if not expected:
        raise GatewayError("platform.misconfigured", "管理令牌未配置", 503)
    if request.headers.get("X-Admin-Token") != expected:
        raise GatewayError("auth.unauthorized", "管理令牌无效", 401)


async def require_tenant(request: Request) -> str | None:
    # 调用方认证（ADR 0011）：Bearer key → config.auth.api_keys 白名单（apikey → tenantId）；
    # 白名单为空 = 不启用认证（本地开发），返回 None（限流按 anonymous 计）。
    api_keys = request.app.state.config.auth.api_keys
    if not api_keys:
        return None
    scheme, _, key = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or key not in api_keys:
        raise GatewayError("auth.invalid_key", "调用方凭据缺失或无效", 401)
    return api_keys[key]


async def tenant_scope(request: Request) -> AsyncIterator[str | None]:
    # 租户资源边界（ADR 0011）：入口处 QPS 令牌桶 + 并发槽，任一不足即 429 快速拒绝。
    # yield 依赖保证并发槽覆盖响应全程（含流式），结束后释放。
    tenant = await require_tenant(request)
    limiter: Limiter = request.app.state.limiter
    limiter.enter_tenant(tenant)
    try:
        yield tenant
    finally:
        limiter.exit_tenant(tenant)

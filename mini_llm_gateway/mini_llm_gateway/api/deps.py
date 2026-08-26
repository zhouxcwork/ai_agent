from __future__ import annotations

import os

from fastapi import Request

from mini_llm_gateway.errors import GatewayError


async def require_admin(request: Request) -> None:
    # 模板写接口的极简鉴权：静态令牌从环境变量注入，与调用方 API 隔离。
    expected = os.getenv(request.app.state.config.admin.token_env)
    if not expected:
        raise GatewayError("gateway_misconfigured", "管理令牌未配置", 503)
    if request.headers.get("X-Admin-Token") != expected:
        raise GatewayError("unauthorized", "管理令牌无效", 401)

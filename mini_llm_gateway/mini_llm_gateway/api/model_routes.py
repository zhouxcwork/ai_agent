from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/v1/models", response_model=None)
async def list_models(http: Request) -> JSONResponse:
    # OpenAI list 形状返回档位；扩展字段 gateway_routes 暴露各路由目标健康状态。
    config = http.app.state.config
    router_state = http.app.state.router.status()
    now = int(time.time())
    data = [
        {"id": mode, "object": "model", "created": now, "owned_by": "llm-gateway"}
        for mode in sorted(config.modes)
    ]
    gateway_routes = {
        mode: [
            {
                "target": target.key,
                "weight": target.weight,
                "enabled": config.providers[target.provider].enabled,
                "healthy": router_state.get(target.key, {}).get("state", "closed") == "closed"
                and config.providers[target.provider].enabled,
                "failures": router_state.get(target.key, {}).get("failures", 0),
                "state": router_state.get(target.key, {}).get("state", "closed"),
            }
            for target in mode_config.targets
        ]
        for mode, mode_config in config.modes.items()
    }
    return JSONResponse({"object": "list", "data": data, "gateway_routes": gateway_routes})

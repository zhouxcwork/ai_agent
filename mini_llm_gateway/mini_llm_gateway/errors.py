from __future__ import annotations

from fastapi.responses import JSONResponse


class GatewayError(Exception):
    # 将内部错误标准化为可安全暴露给调用方的稳定错误码和 HTTP 状态。
    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


async def gateway_error_handler(_: object, exc: GatewayError) -> JSONResponse:
    # 统一错误出口：{"code": "...", "message": "..."}，不泄露内部细节。
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )

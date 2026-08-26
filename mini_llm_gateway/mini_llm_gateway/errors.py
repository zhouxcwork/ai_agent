from __future__ import annotations

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class GatewayError(Exception):
    # 将内部错误标准化为可安全暴露给调用方的稳定错误码和 HTTP 状态。
    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


async def gateway_error_handler(_: object, exc: GatewayError) -> JSONResponse:
    # OpenAI 错误格式：4xx 归 invalid_request_error，5xx 归网关/上游错误。
    error_type = "invalid_request_error" if exc.status_code < 500 else "gateway_error"
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.message, "type": error_type, "param": None, "code": exc.code}},
    )


async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # 参数校验失败（422）同样以 OpenAI 错误格式返回，SDK 可正常抛出。
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "message": "Invalid request",
                "type": "invalid_request_error",
                "param": None,
                "code": "request.validation_error",
                "details": jsonable_encoder(exc.errors()),
            }
        },
    )

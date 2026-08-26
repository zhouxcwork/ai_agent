from __future__ import annotations

import httpx
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

from mini_llm_gateway.provider.base import RetryableUpstreamError, UpstreamRejectedError
from mini_llm_gateway.provider.openai_compatible import _translate_openai_error


def _status_error(cls, status_code: int) -> Exception:
    request = httpx.Request("POST", "http://upstream.test/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return cls("upstream error", response=response, body=None)


def test_transient_5xx_and_429_are_retryable():
    assert isinstance(_translate_openai_error(_status_error(InternalServerError, 500)), RetryableUpstreamError)
    assert isinstance(_translate_openai_error(_status_error(RateLimitError, 429)), RetryableUpstreamError)


def test_rejected_401_403_404_switch_target():
    for cls, status in ((AuthenticationError, 401), (PermissionDeniedError, 403), (NotFoundError, 404)):
        assert isinstance(_translate_openai_error(_status_error(cls, status)), UpstreamRejectedError)


def test_connection_and_timeout_are_retryable():
    request = httpx.Request("POST", "http://upstream.test")
    assert isinstance(_translate_openai_error(APIConnectionError(request=request)), RetryableUpstreamError)
    assert isinstance(_translate_openai_error(APITimeoutError(request=request)), RetryableUpstreamError)


def test_other_4xx_passthrough():
    exc = _status_error(BadRequestError, 400)
    assert _translate_openai_error(exc) is exc

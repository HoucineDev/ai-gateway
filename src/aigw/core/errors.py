"""Error envelope and classification. See docs/spec/01 §2.4 and docs/spec/03 §6."""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorType(str, Enum):
    invalid_request = "invalid_request"
    authentication = "authentication"
    permission = "permission"
    not_found = "not_found"
    rate_limit = "rate_limit"
    budget_exceeded = "budget_exceeded"
    upstream = "upstream"
    unavailable = "unavailable"
    internal = "internal"


_STATUS = {
    ErrorType.invalid_request: 400,
    ErrorType.authentication: 401,
    ErrorType.permission: 403,
    ErrorType.not_found: 404,
    ErrorType.rate_limit: 429,
    ErrorType.budget_exceeded: 429,
    ErrorType.upstream: 502,
    ErrorType.unavailable: 503,
    ErrorType.internal: 500,
}


class GatewayError(Exception):
    def __init__(
        self,
        type_: ErrorType,
        message: str,
        code: str | None = None,
        param: str | None = None,
        headers: dict[str, str] | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.type = type_
        self.message = message
        self.code = code or type_.value
        self.param = param
        self.headers = headers or {}
        self.details = details or {}

    @property
    def status_code(self) -> int:
        return _STATUS[self.type]

    def envelope(self, request_id: str | None = None) -> dict[str, Any]:
        err: dict[str, Any] = {"message": self.message, "type": self.type.value, "code": self.code}
        if self.param:
            err["param"] = self.param
        if request_id:
            err["request_id"] = request_id
        return {"error": err}


class ErrorClass(str, Enum):
    """Upstream failure classification driving retry / fallback / cooldown decisions."""

    invalid_request = "invalid_request"
    authentication = "authentication"
    rate_limited = "rate_limited"
    overloaded = "overloaded"
    timeout_before_send = "timeout_before_send"
    ambiguous = "ambiguous"
    upstream_error = "upstream_error"
    content_filter = "content_filter"


class UpstreamError(Exception):
    def __init__(
        self,
        error_class: ErrorClass,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        upstream_request_id: str | None = None,
        retry_after: float | None = None,
        body: str | None = None,
    ):
        super().__init__(message)
        self.error_class = error_class
        self.message = message
        self.provider = provider
        self.status_code = status_code
        self.upstream_request_id = upstream_request_id
        self.retry_after = retry_after
        self.body = (body or "")[:4096]

    # policy table from docs/spec/04 §6
    @property
    def retry_same(self) -> bool:
        return self.error_class in (ErrorClass.timeout_before_send, ErrorClass.upstream_error)

    @property
    def fallback_allowed(self) -> bool:
        return self.error_class in (
            ErrorClass.rate_limited,
            ErrorClass.overloaded,
            ErrorClass.timeout_before_send,
            ErrorClass.upstream_error,
            ErrorClass.authentication,
        )

    @property
    def cooldown_seconds(self) -> float | None:
        c = self.error_class
        if c == ErrorClass.rate_limited:
            return self.retry_after or 10.0
        if c == ErrorClass.overloaded:
            return 10.0
        if c == ErrorClass.authentication:
            return 300.0
        return None  # timeout/upstream_error use consecutive-failure counters

    def to_gateway_error(self) -> GatewayError:
        if self.error_class == ErrorClass.invalid_request:
            return GatewayError(ErrorType.invalid_request, self.message, code="upstream_rejected")
        if self.error_class == ErrorClass.content_filter:
            return GatewayError(ErrorType.invalid_request, self.message, code="content_filter")
        if self.error_class == ErrorClass.rate_limited:
            return GatewayError(
                ErrorType.unavailable, "All eligible deployments are rate limited", code="upstream_rate_limited"
            )
        return GatewayError(ErrorType.upstream, self.message, code=self.error_class.value)


class UnsupportedParameter(GatewayError):
    def __init__(self, param: str, provider: str):
        super().__init__(
            ErrorType.invalid_request,
            f"Parameter '{param}' is not supported by provider '{provider}' for this deployment",
            code="unsupported_parameter",
            param=param,
        )

from __future__ import annotations


class GatewayError(RuntimeError):
    status_code = 500
    code = "tool_gateway_error"

    def __init__(self, message: str, *, details: object | None = None) -> None:
        super().__init__(message)
        self.details = details


class ToolNotFoundError(GatewayError):
    status_code = 404
    code = "tool_not_found"


class ToolDisabledError(GatewayError):
    status_code = 404
    code = "tool_not_found"


class ToolPermissionError(GatewayError):
    status_code = 403
    code = "tool_permission_denied"


class ToolValidationError(GatewayError):
    status_code = 422
    code = "tool_arguments_invalid"


class IdempotencyRequiredError(GatewayError):
    status_code = 422
    code = "idempotency_key_required"


class IdempotencyConflictError(GatewayError):
    status_code = 409
    code = "idempotency_conflict"


class ApprovalError(GatewayError):
    status_code = 403
    code = "approval_invalid"


class RateLimitError(GatewayError):
    status_code = 429
    code = "tool_rate_limited"


class CircuitOpenError(GatewayError):
    status_code = 503
    code = "tool_circuit_open"


class ToolTimeoutError(GatewayError):
    status_code = 504
    code = "tool_timeout"


class ToolUpstreamError(GatewayError):
    status_code = 502
    code = "tool_upstream_error"

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        details: object | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.retryable = retryable


class ToolOutputError(GatewayError):
    status_code = 502
    code = "tool_output_invalid"


class UnsafeEndpointError(GatewayError):
    status_code = 500
    code = "unsafe_tool_endpoint"

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Callable

from jsonschema import Draft202012Validator

from app.domain.errors import (
    ApprovalError,
    IdempotencyRequiredError,
    ToolOutputError,
    ToolPermissionError,
    ToolTimeoutError,
    ToolUpstreamError,
    ToolValidationError,
)
from app.domain.models import (
    ApprovalRecord,
    ApprovalStatus,
    AuditRecord,
    InvocationContext,
    InvocationRequest,
    InvocationResponse,
    InvocationStatus,
    ToolSpec,
)
from app.infrastructure.repository import SqliteRepository
from app.registry import ToolRegistry
from app.resilience import CircuitBreaker, FixedWindowRateLimiter


class ToolExecutionService:
    def __init__(
        self,
        registry: ToolRegistry,
        repository: SqliteRepository,
        *,
        approval_ttl_seconds: int,
        idempotency_ttl_seconds: int,
        event_publisher: Callable[[InvocationResponse, InvocationContext], None] | None = None,
    ) -> None:
        self.registry = registry
        self.repository = repository
        self.approval_ttl_seconds = approval_ttl_seconds
        self.idempotency_ttl_seconds = idempotency_ttl_seconds
        self.event_publisher = event_publisher
        self.rate_limiter = FixedWindowRateLimiter()
        self.circuit_breaker = CircuitBreaker()

    async def invoke(
        self,
        name: str,
        payload: InvocationRequest,
        context: InvocationContext,
    ) -> InvocationResponse:
        spec, adapter = self.registry.resolve(name, payload.version)
        preflight_started = monotonic()
        claimed = False
        try:
            self.registry.assert_visible(spec, context.tenant_id)
            self._authorize(spec, context)
            self._validate_json(spec.input_schema, payload.arguments, arguments=True)
            request_hash = _request_hash(spec, payload.arguments, context)

            if spec.requires_idempotency_key and not context.idempotency_key:
                raise IdempotencyRequiredError("write tools require X-Idempotency-Key")

            if context.idempotency_key:
                replay = self.repository.find_idempotency(
                    context.tenant_id,
                    spec.name,
                    context.idempotency_key,
                    request_hash,
                )
                if replay is not None:
                    response = replay.model_copy(update={"idempotent_replay": True})
                    self._audit(response, spec, context, payload.arguments)
                    return response

            if spec.approval_required:
                pending = self._authorize_approval(spec, payload, context, request_hash)
                if pending is not None:
                    return pending

            if context.idempotency_key:
                claim = self.repository.claim_idempotency(
                    context.tenant_id,
                    spec.name,
                    context.idempotency_key,
                    request_hash,
                    datetime.now(UTC) + timedelta(seconds=self.idempotency_ttl_seconds),
                )
                if claim.outcome == "REPLAY":
                    response = claim.response.model_copy(update={"idempotent_replay": True})
                    self._audit(response, spec, context, payload.arguments)
                    return response
                claimed = True
        except Exception as exc:
            failed = InvocationResponse(
                status=InvocationStatus.FAILED,
                tool_name=spec.name,
                tool_version=spec.version,
                duration_ms=int((monotonic() - preflight_started) * 1_000),
            )
            self._audit(
                failed,
                spec,
                context,
                payload.arguments,
                error_type=type(exc).__name__,
            )
            raise

        started = monotonic()
        invocation = InvocationResponse(
            status=InvocationStatus.SUCCEEDED,
            tool_name=spec.name,
            tool_version=spec.version,
        )
        attempts = 0
        try:
            limiter_key = f"{context.tenant_id}:{spec.name}:{spec.version}"
            breaker_key = f"{spec.name}:{spec.version}"
            self.rate_limiter.acquire(limiter_key, spec.rate_limit_per_minute)
            self.circuit_breaker.allow(breaker_key, spec.breaker_reset_seconds)
            output, attempts = await self._execute_with_retry(
                spec,
                adapter,
                payload.arguments,
                context,
            )
            self._validate_json(spec.output_schema, output, arguments=False)
            invocation.output = output
            invocation.attempt_count = attempts
            invocation.duration_ms = int((monotonic() - started) * 1_000)
            self.circuit_breaker.record_success(breaker_key)
            if context.idempotency_key:
                self.repository.complete_idempotency(
                    context.tenant_id,
                    spec.name,
                    context.idempotency_key,
                    invocation,
                )
            if payload.approval_id:
                self.repository.consume_approval(payload.approval_id)
            self._audit(invocation, spec, context, payload.arguments)
            return invocation
        except Exception as exc:
            attempts = int(getattr(exc, "attempt_count", attempts))
            duration_ms = int((monotonic() - started) * 1_000)
            invocation.status = InvocationStatus.FAILED
            invocation.attempt_count = attempts
            invocation.duration_ms = duration_ms
            if isinstance(exc, (ToolUpstreamError, ToolTimeoutError)):
                self.circuit_breaker.record_failure(
                    f"{spec.name}:{spec.version}",
                    spec.breaker_failure_threshold,
                )
            if claimed and context.idempotency_key:
                self.repository.release_idempotency(
                    context.tenant_id,
                    spec.name,
                    context.idempotency_key,
                )
            self._audit(
                invocation,
                spec,
                context,
                payload.arguments,
                error_type=type(exc).__name__,
            )
            raise

    def _authorize_approval(
        self,
        spec: ToolSpec,
        payload: InvocationRequest,
        context: InvocationContext,
        request_hash: str,
    ) -> InvocationResponse | None:
        if not payload.approval_id:
            approval = self.repository.get_or_create_approval(
                ApprovalRecord(
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    tool_name=spec.name,
                    tool_version=spec.version,
                    request_hash=request_hash,
                    expires_at=datetime.now(UTC) + timedelta(seconds=self.approval_ttl_seconds),
                )
            )
            response = InvocationResponse(
                status=InvocationStatus.PENDING_APPROVAL,
                tool_name=spec.name,
                tool_version=spec.version,
                approval_id=approval.approval_id,
            )
            self._audit(response, spec, context, payload.arguments)
            return response
        approval = self.repository.get_approval(payload.approval_id)
        if approval is None:
            raise ApprovalError("approval does not exist")
        if approval.status != ApprovalStatus.APPROVED:
            raise ApprovalError(f"approval is not approved: {approval.status}")
        if (
            approval.tenant_id != context.tenant_id
            or approval.user_id != context.user_id
            or approval.tool_name != spec.name
            or approval.tool_version != spec.version
            or approval.request_hash != request_hash
        ):
            raise ApprovalError("approval does not match this invocation")
        return None

    async def _execute_with_retry(
        self,
        spec: ToolSpec,
        adapter: Any,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> tuple[Any, int]:
        attempts = spec.retry_attempts if spec.idempotent else 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                output = await asyncio.wait_for(
                    adapter.execute(arguments, context),
                    timeout=spec.timeout_seconds,
                )
                return output, attempt
            except TimeoutError as exc:
                last_error = ToolTimeoutError(f"tool exceeded {spec.timeout_seconds:g} seconds")
                last_error.attempt_count = attempt
                if attempt == attempts:
                    raise last_error from exc
            except ToolUpstreamError as exc:
                last_error = exc
                exc.attempt_count = attempt
                if not exc.retryable or attempt == attempts:
                    raise
            except Exception as exc:
                exc.attempt_count = attempt
                raise
            if attempt < attempts:
                await asyncio.sleep(min(0.05 * (2 ** (attempt - 1)), 0.5))
        raise last_error or ToolUpstreamError("tool execution failed")

    @staticmethod
    def _authorize(spec: ToolSpec, context: InvocationContext) -> None:
        missing = sorted(set(spec.required_permissions) - context.permissions)
        if missing:
            raise ToolPermissionError(
                f"missing permissions for tool {spec.name}: {', '.join(missing)}"
            )

    @staticmethod
    def _validate_json(
        schema: dict[str, Any] | None,
        instance: Any,
        *,
        arguments: bool,
    ) -> None:
        if schema is None:
            return
        errors = sorted(
            Draft202012Validator(schema).iter_errors(instance),
            key=lambda item: list(item.absolute_path),
        )
        if not errors:
            return
        details = [
            {
                "path": ".".join(str(part) for part in error.absolute_path),
                "message": error.message,
            }
            for error in errors[:20]
        ]
        if arguments:
            raise ToolValidationError(
                "tool arguments do not match the input schema",
                details=details,
            )
        raise ToolOutputError("tool output does not match the output schema", details=details)

    def _audit(
        self,
        invocation: InvocationResponse,
        spec: ToolSpec,
        context: InvocationContext,
        arguments: dict[str, Any],
        *,
        error_type: str = "",
    ) -> None:
        self.repository.append_audit(
            AuditRecord(
                invocation_id=invocation.invocation_id,
                request_id=context.request_id,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                tool_name=spec.name,
                tool_version=spec.version,
                status=invocation.status,
                attempt_count=invocation.attempt_count,
                duration_ms=invocation.duration_ms,
                arguments_sha256=_sha256_json(arguments),
                idempotency_key_sha256=(
                    _sha256_text(context.idempotency_key) if context.idempotency_key else ""
                ),
                error_type=error_type,
            )
        )
        if self.event_publisher is not None:
            self.event_publisher(invocation, context)


def _request_hash(
    spec: ToolSpec,
    arguments: dict[str, Any],
    context: InvocationContext,
) -> str:
    return _sha256_json(
        {
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "tool": spec.name,
            "version": spec.version,
            "arguments": arguments,
        }
    )


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(encoded)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

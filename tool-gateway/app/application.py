"""Single security boundary for externally observable tool side effects."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

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
from app.policy_evaluator import ToolPolicyEvaluator
from app.registry import ToolRegistry
from app.resilience import CircuitBreaker, FixedWindowRateLimiter


class ToolExecutionService:
    """Validates, authorizes, executes and audits a versioned tool invocation.

    The ordering is deliberate: input and permission checks happen before an
    adapter is called; approval consumption happens before the side effect;
    output validation and auditing happen for both success and failure paths.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        repository: SqliteRepository,
        *,
        approval_ttl_seconds: int,
        idempotency_ttl_seconds: int,
        event_publisher: Callable[[InvocationResponse, InvocationContext, ToolSpec, bool], None]
        | None = None,
        rate_limiter=None,
        policy_authorizer=None,
    ) -> None:
        """注入目录、仓储和治理发布器, 并初始化限流与熔断执行保护。"""
        self.registry = registry
        self.repository = repository
        self.approval_ttl_seconds = approval_ttl_seconds
        self.idempotency_ttl_seconds = idempotency_ttl_seconds
        self.event_publisher = event_publisher
        self.rate_limiter = rate_limiter or FixedWindowRateLimiter()
        self.policy_authorizer = policy_authorizer
        self.policy_evaluator = ToolPolicyEvaluator(registry, policy_authorizer)
        self.circuit_breaker = CircuitBreaker()

    async def invoke(
        self,
        name: str,
        payload: InvocationRequest,
        context: InvocationContext,
    ) -> InvocationResponse:
        """按‘目录解析、身份与策略授权、Schema 校验、审批、幂等占用、限流熔断、执行
        、审计’的固定顺序调用工具；任何前置门禁失败都不得触发外部副作用。

        Execute only after policy, schema, approval and idempotency gates pass.
        """
        spec, adapter = self.registry.resolve(name, payload.version)
        preflight_started = monotonic()
        claimed = False
        try:
            now = datetime.now(UTC)
            if context.deadline_at is not None and now >= context.deadline_at:
                raise ToolTimeoutError("execution deadline has expired")
            if (
                context.attempt_budget_remaining is not None
                and context.attempt_budget_remaining <= 0
            ):
                raise ToolUpstreamError("downstream attempt budget is exhausted", retryable=False)
            await self.policy_evaluator.evaluate(
                spec,
                context,
                payload.arguments,
                validate_input=lambda schema, value: self._validate_json(
                    schema, value, arguments=True
                ),
            )
            if context.connector_id and (
                not context.connector_grant or not self.repository.consume_connector_grant(
                    context.tenant_id, context.user_id, context.connector_id,
                    context.connector_grant, context.run_id, context.snapshot_id,
                    spec.name, spec.version,
                )
            ):
                raise ToolPermissionError(
                    "connector grant is missing, expired, or already consumed"
                )
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
                    self._audit(
                        response,
                        spec,
                        context,
                        payload.arguments,
                        approval_granted=bool(payload.approval_id),
                    )
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
                    self._audit(
                        response,
                        spec,
                        context,
                        payload.arguments,
                        approval_granted=bool(payload.approval_id),
                    )
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
            authorization=self._authorization_facts(spec, context, decision="ALLOW"),
        )
        attempts = 0
        try:
            limiter_key = f"{context.tenant_id}:{spec.name}:{spec.version}"
            breaker_key = f"{spec.name}:{spec.version}"
            self.rate_limiter.acquire(limiter_key, spec.rate_limit_per_minute)
            self.circuit_breaker.allow(breaker_key, spec.breaker_reset_seconds)
            if self._must_simulate(spec, context):
                output, attempts = self._simulated_output(spec, payload.arguments, context), 1
            else:
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
            self._audit(
                invocation,
                spec,
                context,
                payload.arguments,
                approval_granted=bool(payload.approval_id),
            )
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
                approval_granted=bool(payload.approval_id),
            )
            raise

    @staticmethod
    def _must_simulate(spec: ToolSpec, context: InvocationContext) -> bool:
        """Shadow 只允许真实只读调用；实验 simulated 模式绝不触达任何外部 Adapter。"""
        return context.execution_mode == "simulated" or (
            context.execution_mode == "shadow" and spec.risk.value != "read_only"
        )

    @staticmethod
    def _simulated_output(
        spec: ToolSpec, arguments: dict[str, Any], context: InvocationContext
    ) -> Any:
        """解释固定故障脚本或返回 Dry-run receipt；任何脚本都不能请求真实上游。"""
        profile = context.simulation_profile
        fault = str(profile.get("fault", "")).lower()
        if fault in {"timeout", "500", "upstream_error", "permission_denied"}:
            raise ToolUpstreamError(f"simulated tool fault: {fault}", retryable=fault != "permission_denied")
        if "output" in profile:
            return profile["output"]
        return {
            "simulated": True,
            "would_execute": spec.name,
            "arguments_sha256": hashlib.sha256(
                json.dumps(arguments, sort_keys=True, default=str).encode()
            ).hexdigest(),
            "execution_mode": context.execution_mode,
        }

    async def record_connector_result(
        self, name: str, version: str, task_id: str, result_sha256: str, context: InvocationContext
    ) -> InvocationResponse:
        """消费本机 Connector 的单次 Grant 并记录统一工具审计，不重复执行适配器。"""
        spec, _ = self.registry.resolve(name, version)
        self.registry.assert_visible(spec, context.tenant_id)
        self._authorize(spec, context)
        self._validate_runtime_action_identity(spec, context)
        if not context.connector_id or not context.connector_grant:
            raise ToolPermissionError("connector identity and grant are required")
        receipt = self.repository.connector_result_receipt(
            context.tenant_id, context.connector_id, task_id, result_sha256
        )
        if receipt is not None:
            return InvocationResponse(
                invocation_id=receipt, status=InvocationStatus.SUCCEEDED,
                tool_name=spec.name, tool_version=spec.version,
                output={"task_id": task_id, "result_sha256": result_sha256},
                idempotent_replay=True,
            )
        if not self.repository.consume_connector_grant(
            context.tenant_id, context.user_id, context.connector_id, context.connector_grant,
            context.run_id, context.snapshot_id, spec.name, spec.version,
        ):
            raise ToolPermissionError("connector grant is missing, expired, or already consumed")
        invocation = InvocationResponse(
            status=InvocationStatus.SUCCEEDED, tool_name=spec.name, tool_version=spec.version,
            output={"task_id": task_id, "result_sha256": result_sha256},
        )
        self._audit(invocation, spec, context, {"task_id": task_id, "result_sha256": result_sha256})
        self.repository.save_connector_result_receipt(
            context.tenant_id,
            context.connector_id,
            task_id,
            result_sha256,
            invocation.invocation_id,
        )
        return invocation

    def _authorize_approval(
        self,
        spec: ToolSpec,
        payload: InvocationRequest,
        context: InvocationContext,
        request_hash: str,
    ) -> InvocationResponse | None:
        """校验审批是否与租户、工具版本和请求摘要精确绑定，并原子消费一次性审批；未获批时返
        回待审批状态而不执行工具。

        Create or atomically consume an approval bound to this exact request hash.
        """
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
                authorization=self._authorization_facts(spec, context, decision="REQUIRE_APPROVAL"),
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
        # Consume before the side effect. This is fail-closed and prevents two
        # concurrent requests from executing under the same approval.
        self.repository.consume_approval(payload.approval_id)
        return None

    @staticmethod
    def _authorization_facts(
        spec: ToolSpec,
        context: InvocationContext,
        *,
        decision: str,
    ) -> dict[str, Any]:
        """生成动作级授权投影; 作为事件和响应共享的可审计事实。"""
        return {
            "operation_id": context.operation_id or context.tool_execution_id,
            "step_id": context.step_id,
            "plan_id": context.plan_id,
            "admission_id": context.plan_admission_id,
            "decision": decision,
            "tool": f"{spec.name}:{spec.version}",
            "risk": spec.risk.value,
        }

    async def _execute_with_retry(
        self,
        spec: ToolSpec,
        adapter: Any,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> tuple[Any, int]:
        """在 Deadline
        与剩余尝试预算内执行工具适配器；仅重试声明为可重试的上游错误。

        Retry only declared-idempotent tools within the caller's remaining deadline.
        """
        attempts = spec.retry_attempts if spec.idempotent else 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                timeout = spec.timeout_seconds
                if context.deadline_at is not None:
                    remaining = (context.deadline_at - datetime.now(UTC)).total_seconds()
                    if remaining <= 0:
                        raise ToolTimeoutError("execution deadline has expired")
                    timeout = min(timeout, remaining)
                output = await asyncio.wait_for(
                    adapter.execute(arguments, context),
                    timeout=timeout,
                )
                return output, attempt
            except TimeoutError as exc:
                last_error = ToolTimeoutError(f"tool exceeded {timeout:g} seconds")
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
    def _validate_json(
        schema: dict[str, Any] | None,
        instance: Any,
        *,
        arguments: bool,
    ) -> None:
        """按工具版本绑定的 JSON Schema
        校验输入或输出；失败时不进入下一副作用阶段。

        Validate untrusted input or adapter output and expose only bounded schema errors.
        """
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
        approval_granted: bool = False,
    ) -> None:
        """对成功、失败、审批和幂等重放统一生成审计记录与治理事件，并对敏感参数仅保存摘要。

        Persist the immutable invocation record before asynchronously publishing governance.
        """
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
            self.event_publisher(invocation, context, spec, approval_granted)


def _request_hash(
    spec: ToolSpec,
    arguments: dict[str, Any],
    context: InvocationContext,
) -> str:
    """将工具版本、参数、租户和执行上下文规范化后计算请求摘要，用于审批绑定和幂等冲突检测。

    Bind approval and idempotency to tenant, user, tool version and exact arguments.
    """
    return _sha256_json(
        {
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "tool": spec.name,
            "version": spec.version,
            "operation_id": context.operation_id,
            "step_id": context.step_id,
            "plan_id": context.plan_id,
            "plan_admission_id": context.plan_admission_id,
            "arguments": arguments,
        }
    )


def _sha256_json(value: Any) -> str:
    """对排序后的紧凑 JSON 计算 SHA-256，保证跨进程对同一结构得到一致摘要。

    Canonicalise JSON so semantically identical argument maps hash identically.
    """
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(encoded)


def _sha256_text(value: str) -> str:
    """对 UTF-8 文本计算 SHA-256，避免在审计与日志中直接持久化敏感原文。

    Return a non-reversible audit correlation digest rather than raw sensitive data.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

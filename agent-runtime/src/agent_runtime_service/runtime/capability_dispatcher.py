"""统一业务能力分派边界。

Planner 只提出 Capability；本模块从冻结目录选择 Provider，校验 Schema，
再调用启动期注册的类型处理器。处理器仍必须通过各 Gateway，分派器不实现副作用。
"""

from collections.abc import Callable
from typing import Any

from jsonschema import Draft202012Validator
from platform_sdk.contracts.execution import ExecutionContext
from platform_sdk.contracts.skills import (
    CapabilityProviderDescriptor,
    CapabilityProviderKind,
    CapabilityResolutionRequest,
    CapabilityRoutingPolicy,
)
from pydantic import BaseModel

from agent_runtime_service.runtime.capability_health import (
    CapabilityHealthMonitor,
    ProviderHealthReport,
)
from agent_runtime_service.runtime.capability_resolver import BusinessCapabilityResolver
from agent_runtime_service.runtime.workflow_runtime import WorkflowSuspended

ProviderHandler = Callable[
    [CapabilityProviderDescriptor, dict[str, Any], ExecutionContext], dict[str, Any]
]


class CapabilityDispatchError(RuntimeError):
    """能力无合法 Provider、无执行器或契约漂移时失败关闭。"""


class CapabilityDispatchResult(BaseModel):
    """可审计的 Provider 选择与结构化输出。"""

    capability_id: str
    provider_id: str
    provider_kind: CapabilityProviderKind
    provider_version: str
    output: dict[str, Any]


class GovernedCapabilityDispatcher:
    """依冻结回退链解析并调用 Tool/Skill/Agent/Human/RAG/Workflow。"""

    def __init__(
        self,
        providers: list[CapabilityProviderDescriptor],
        policies: list[CapabilityRoutingPolicy],
        handlers: dict[CapabilityProviderKind, ProviderHandler],
        health_reports: list[ProviderHealthReport] | None = None,
    ) -> None:
        """冻结目录、逐能力顺序和处理器，请求期不允许注册新 Provider。"""
        parsed_providers = [CapabilityProviderDescriptor.model_validate(item) for item in providers]
        if health_reports:
            parsed_providers = CapabilityHealthMonitor().apply(parsed_providers, health_reports)
        self._resolver = BusinessCapabilityResolver(parsed_providers)
        self._policies = {item.capability_id: item for item in policies}
        if len(self._policies) != len(policies):
            raise ValueError("capability routing policies must be unique")
        self._handlers = dict(handlers)

    def dispatch(
        self,
        capability_id: str,
        payload: dict[str, Any],
        context: ExecutionContext,
        *,
        caller_permissions: frozenset[str] = frozenset(),
        max_cost_usd: float = 0.0,
        max_latency_ms: int = 0,
        require_independent_authority: bool = False,
    ) -> CapabilityDispatchResult:
        """执行硬约束、按能力回退、Schema 校验和类型分派。"""
        request = CapabilityResolutionRequest(
            capability_id=capability_id,
            caller_permissions=caller_permissions,
            max_cost_usd=max_cost_usd,
            max_latency_ms=max_latency_ms,
            require_independent_authority=require_independent_authority,
        )
        policy = self._policies.get(request.capability_id)
        candidates = self._resolver.candidates(request, policy)
        if not candidates:
            raise CapabilityDispatchError(
                f"no governed provider is available for capability: {request.capability_id}"
            )
        last_error: Exception | None = None
        for index, provider in enumerate(candidates):
            handler = self._handlers.get(provider.kind)
            if handler is None:
                last_error = CapabilityDispatchError(
                    f"provider kind is not deployed in this runtime: {provider.kind.value}"
                )
            else:
                try:
                    self._validate_schema(
                        provider.input_schema, payload, "input", provider.provider_id
                    )
                    output = handler(provider, payload, context)
                    if not isinstance(output, dict):
                        raise CapabilityDispatchError("capability provider must return an object")
                    self._validate_schema(
                        provider.output_schema, output, "output", provider.provider_id
                    )
                    return CapabilityDispatchResult(
                        capability_id=request.capability_id,
                        provider_id=provider.provider_id,
                        provider_kind=provider.kind,
                        provider_version=provider.version,
                        output=output,
                    )
                except WorkflowSuspended:
                    raise
                except Exception as exc:
                    last_error = exc
            if index == len(candidates) - 1 or not provider.fallback_safe:
                break
        if isinstance(last_error, CapabilityDispatchError):
            raise last_error
        raise CapabilityDispatchError(
            f"capability provider execution failed: {request.capability_id}"
        ) from last_error

    def dispatch_output(
        self,
        capability_id: str,
        payload: dict[str, Any],
        context: ExecutionContext,
        **constraints: Any,
    ) -> dict[str, Any]:
        """为 Workflow 步骤协议返回纯输出，Provider 选择仍由 ``dispatch`` 审计。"""
        return self.dispatch(capability_id, payload, context, **constraints).output

    @staticmethod
    def _validate_schema(
        schema: dict[str, Any], value: dict[str, Any], direction: str, provider_id: str
    ) -> None:
        """在 Provider 边界前后验证发布 Schema，不对错误输出宽容转换。"""
        if not schema:
            return
        errors = list(Draft202012Validator(schema).iter_errors(value))
        if errors:
            raise CapabilityDispatchError(
                f"provider '{provider_id}' {direction} violates schema: {errors[0].message}"
            )

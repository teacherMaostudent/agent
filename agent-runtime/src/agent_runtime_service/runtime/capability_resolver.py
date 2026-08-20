"""业务 Capability 到受控 Provider 的确定性解析器。"""

from __future__ import annotations

from platform_sdk.contracts.skills import (
    CapabilityProviderDescriptor,
    CapabilityProviderKind,
    CapabilityResolutionRequest,
    CapabilityRoutingPolicy,
    ProviderHealthStatus,
)


class CapabilityResolutionError(RuntimeError):
    """没有符合发布、权限、健康和资源约束的 Provider 时抛出。"""


class BusinessCapabilityResolver:
    """在冻结目录中选择 Provider；不发起 LLM 调用，也不执行 Provider 本身。"""

    def __init__(self, providers: list[CapabilityProviderDescriptor]) -> None:
        """保存启动期或快照期冻结的 Provider 目录，拒绝请求期动态注册。"""
        identifiers = [item.provider_id for item in providers]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("capability provider IDs must be unique")
        self._providers = tuple(providers)

    def resolve(self, request: CapabilityResolutionRequest) -> CapabilityProviderDescriptor:
        """按资格、健康、权限、预算、SLA 与业务优先级选择唯一 Provider。"""
        candidates = [item for item in self._providers if self._matches(item, request)]
        if not candidates:
            raise CapabilityResolutionError(
                f"no governed provider is available for capability: {request.capability_id}"
            )
        return min(candidates, key=lambda item: self._rank(item, request))

    def candidates(
        self,
        request: CapabilityResolutionRequest,
        policy: CapabilityRoutingPolicy | None = None,
    ) -> list[CapabilityProviderDescriptor]:
        """返回全部合法候选，供执行期仅对显式可回退 Provider 切换。"""
        if policy is None:
            return sorted(
                [item for item in self._providers if self._matches(item, request)],
                key=lambda item: self._rank(item, request),
            )
        if policy.capability_id != request.capability_id:
            raise CapabilityResolutionError("capability routing policy does not match request")
        by_id = {item.provider_id: item for item in self._providers}
        result = []
        for provider_id in policy.provider_order:
            item = by_id.get(provider_id)
            if item is None:
                continue
            if item.health_status == ProviderHealthStatus.DEGRADED and not policy.allow_degraded:
                continue
            if self._matches(item, request):
                result.append(item)
        return result

    def resolve_with_policy(
        self, request: CapabilityResolutionRequest, policy: CapabilityRoutingPolicy
    ) -> CapabilityProviderDescriptor:
        """按照能力自身的发布顺序选择首个合法 Provider，而不是使用全局固定顺序。"""
        if policy.capability_id != request.capability_id:
            raise CapabilityResolutionError("capability routing policy does not match request")
        by_id = {item.provider_id: item for item in self._providers}
        for provider_id in policy.provider_order:
            item = by_id.get(provider_id)
            if item is None:
                continue
            if item.health_status == ProviderHealthStatus.DEGRADED and not policy.allow_degraded:
                continue
            if self._matches(item, request):
                return item
        raise CapabilityResolutionError(
            f"no policy-ordered provider is available for capability: {request.capability_id}"
        )

    @staticmethod
    def _matches(item: CapabilityProviderDescriptor, request: CapabilityResolutionRequest) -> bool:
        """先执行所有硬约束；不以平均分或模型偏好掩盖安全拒绝条件。"""
        if (
            not item.qualified
            or not item.healthy
            or item.health_status
            in {
                ProviderHealthStatus.UNAVAILABLE,
                ProviderHealthStatus.QUARANTINED,
            }
        ):
            return False
        if request.capability_id not in {value.capability_id for value in item.capabilities}:
            return False
        if not set(item.required_permissions) <= set(request.caller_permissions):
            return False
        if request.max_cost_usd and item.max_cost_usd > request.max_cost_usd:
            return False
        if request.max_latency_ms and item.max_latency_ms > request.max_latency_ms:
            return False
        return not request.require_independent_authority or item.kind in {
            CapabilityProviderKind.AGENT,
            CapabilityProviderKind.HUMAN,
        }

    @staticmethod
    def _rank(
        item: CapabilityProviderDescriptor, request: CapabilityResolutionRequest
    ) -> tuple[int, int, int, str]:
        """只对已合法候选排序，优先业务发布优先级，再比较成本和延迟。"""
        del request
        return (item.priority, item.max_cost_usd > 0, item.max_latency_ms, item.provider_id)

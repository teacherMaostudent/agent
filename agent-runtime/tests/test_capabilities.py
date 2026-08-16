import pytest
from platform_sdk.contracts.capabilities import CapabilityManifest, RuntimeCapability

from agent_runtime_service.runtime.capabilities import CapabilityRegistry, CapabilityUnavailable


def test_capability_registry_is_startup_frozen_and_rejects_missing_plan_dependency() -> None:
    """能力目录只反映启动装配结果；发布计划不能把未部署能力带入执行。"""
    context = object()
    registry = CapabilityRegistry(
        {RuntimeCapability.CONTEXT: context}, version="runtime-capability-catalog/v1"
    )

    assert registry.names == ("context",)
    assert registry.require(RuntimeCapability.CONTEXT) is context
    with pytest.raises(CapabilityUnavailable, match="llm"):
        registry.validate(["context", "llm"])


def test_capability_registry_rejects_unknown_capability_name_at_startup() -> None:
    """拼写错误的能力不得在运行中成为隐式、无法治理的扩展点。"""
    with pytest.raises(ValueError, match="unknown runtime capability"):
        CapabilityRegistry({"unbounded_plugin": object()}, version="catalog/v1")


def test_capability_manifest_proves_provider_identity_and_profile_compatibility() -> None:
    """同名能力也必须有可审计 Provider 摘要，并能拒绝错误执行器的组合。"""
    registry = CapabilityRegistry(
        {RuntimeCapability.LLM: object()},
        version="catalog/v2",
        manifests=[
            CapabilityManifest(
                capability=RuntimeCapability.LLM,
                provider_id="gateway.primary",
                artifact_digest="a" * 64,
                executor_profiles=("declarative-langgraph/v1",),
            )
        ],
    )

    assert len(registry.manifest_digest) == 64
    registry.validate_profiles(["llm"], "declarative-langgraph/v1")
    with pytest.raises(CapabilityUnavailable, match="does not support"):
        registry.validate_profiles(["llm"], "temporal-workflow/v1")

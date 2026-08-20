import pytest
from platform_sdk.contracts.skills import (
    CapabilityProviderDescriptor,
    CapabilityProviderKind,
    CapabilityResolutionRequest,
    CapabilityRoutingPolicy,
)

from agent_runtime_service.runtime.capability_resolver import (
    BusinessCapabilityResolver,
    CapabilityResolutionError,
)


def _provider(identifier: str, kind: CapabilityProviderKind, priority: int, **extra):
    return CapabilityProviderDescriptor(
        provider_id=identifier,
        kind=kind,
        version="1",
        priority=priority,
        capabilities=[{"capability_id": "LEGAL_REVIEW"}],
        **extra,
    )


def test_resolver_prefers_qualified_skill_but_requires_agent_for_independent_authority():
    resolver = BusinessCapabilityResolver(
        [
            _provider("legal-skill", CapabilityProviderKind.SKILL, 10),
            _provider("legal-agent", CapabilityProviderKind.AGENT, 20),
        ]
    )
    assert (
        resolver.resolve(CapabilityResolutionRequest(capability_id="legal_review")).provider_id
        == "legal-skill"
    )
    assert (
        resolver.resolve(
            CapabilityResolutionRequest(
                capability_id="LEGAL_REVIEW", require_independent_authority=True
            )
        ).provider_id
        == "legal-agent"
    )


def test_resolver_fails_closed_when_only_unhealthy_provider_matches():
    resolver = BusinessCapabilityResolver(
        [_provider("review", CapabilityProviderKind.SKILL, 1, healthy=False)]
    )
    with pytest.raises(CapabilityResolutionError, match="no governed provider"):
        resolver.resolve(CapabilityResolutionRequest(capability_id="LEGAL_REVIEW"))


def test_fallback_order_is_owned_by_each_capability_policy():
    resolver = BusinessCapabilityResolver(
        [
            _provider("human", CapabilityProviderKind.HUMAN, 100),
            _provider("skill", CapabilityProviderKind.SKILL, 1),
        ]
    )
    request = CapabilityResolutionRequest(capability_id="LEGAL_REVIEW")
    selected = resolver.resolve_with_policy(
        request,
        CapabilityRoutingPolicy(capability_id="LEGAL_REVIEW", provider_order=["human", "skill"]),
    )
    assert selected.provider_id == "human"


def test_rag_provider_must_freeze_index_and_embedding_contract():
    """RAG Provider 不能只绑名称，否则重建索引后运行结果会漂移。"""
    with pytest.raises(ValueError, match="rag_index_version"):
        _provider("knowledge", CapabilityProviderKind.RAG, 1)
    provider = _provider(
        "knowledge",
        CapabilityProviderKind.RAG,
        1,
        rag_index_version="index-v7",
        embedding_contract_id="bge-m3-1024-v1",
    )
    assert provider.rag_index_version == "index-v7"

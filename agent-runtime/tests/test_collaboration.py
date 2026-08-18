"""能力优先协作层的确定性测试。"""

import pytest
from platform_sdk.contracts.subagents import (
    AgentResult,
    CapabilityRequirement,
    ConflictStrategy,
    SubAgentBinding,
)

from agent_runtime_service.runtime.collaboration import (
    CapabilityAvailability,
    CapabilityRouter,
    CollaborationError,
    ResultResolver,
    structured_agent_result,
)


def test_capability_router_selects_authorized_healthy_provider() -> None:
    """Planner 请求能力时，Router 应选择满足 Schema/地域/调用方约束的 Provider。"""
    selected = CapabilityRouter().select(
        [
            SubAgentBinding(
                agent_id="legal-primary",
                capabilities=["LEGAL_REVIEW"],
                allowed_callers=["compliance-agent"],
                authority_rank=10,
            ),
            SubAgentBinding(
                agent_id="legal-backup",
                capabilities=["LEGAL_REVIEW"],
                allowed_callers=["compliance-agent"],
                authority_rank=20,
                fallback_compatible=True,
            ),
        ],
        CapabilityRequirement(capability_id="legal_review"),
        caller_agent_id="compliance-agent",
    )

    assert selected.binding.agent_id == "legal-primary"
    assert selected.availability is CapabilityAvailability.AVAILABLE


def test_capability_router_fails_closed_for_unauthorized_or_missing_provider() -> None:
    """能力名称匹配但调用方未获授权时，不允许把它当作自动 fallback。"""
    with pytest.raises(CollaborationError, match="capability unavailable"):
        CapabilityRouter().select(
            [SubAgentBinding(agent_id="legal", capabilities=["LEGAL_REVIEW"], allowed_callers=["other"])],
            CapabilityRequirement(capability_id="LEGAL_REVIEW"),
            caller_agent_id="compliance-agent",
        )


def test_capability_router_selects_only_the_published_parallel_provider_group() -> None:
    """并行度和裁决策略由 Binding 冻结；少一个健康专家就不能静默缩减为单 Agent。"""
    bindings = [
        SubAgentBinding(
            agent_id="expert-a",
            capabilities=["RISK_REVIEW"],
            parallelism=2,
            conflict_strategy=ConflictStrategy.QUORUM,
            authority_rank=10,
        ),
        SubAgentBinding(
            agent_id="expert-b",
            capabilities=["RISK_REVIEW"],
            parallelism=2,
            conflict_strategy=ConflictStrategy.QUORUM,
            authority_rank=20,
        ),
    ]

    selections = CapabilityRouter().select_group(
        bindings,
        CapabilityRequirement(capability_id="risk_review"),
        caller_agent_id="coordinator",
    )

    assert [item.binding.agent_id for item in selections] == ["expert-a", "expert-b"]
    with pytest.raises(CollaborationError, match="requires 2 healthy providers"):
        CapabilityRouter().select_group(
            bindings,
            CapabilityRequirement(capability_id="risk_review"),
            caller_agent_id="coordinator",
            unavailable_agents=frozenset({"expert-b"}),
        )


def test_result_resolver_requires_escalation_when_quorum_is_not_reached() -> None:
    """冲突结果不能靠最后返回或更高置信度硬选，无法形成多数时必须升级。"""
    results = [
        AgentResult(decision="HIGH", confidence=0.9, provider_agent_id="legal", provider_snapshot_id="l7"),
        AgentResult(decision="LOW", confidence=0.95, provider_agent_id="quality", provider_snapshot_id="q3"),
    ]

    assert ResultResolver().resolve(results, ConflictStrategy.QUORUM) is None


def test_runtime_result_is_reduced_to_structured_cross_agent_fact() -> None:
    """仲裁器只比较 Provider 身份、决策、证据和版本，不依赖未限制的子 Agent 全文。"""
    item = structured_agent_result(
        {
            "status": "COMPLETED",
            "termination_reason": "HIGH_RISK",
            "answer": "Evidence indicates a high risk.",
            "snapshot_id": "legal-v7",
            "evidence": [{"source_id": "law-27"}],
        },
        binding=SubAgentBinding(agent_id="legal", authority_rank=10),
        provider_agent_id="legal",
    )

    assert item.decision == "HIGH_RISK"
    assert item.evidence_ids == ["law-27"]
    assert item.authority_rank == 10

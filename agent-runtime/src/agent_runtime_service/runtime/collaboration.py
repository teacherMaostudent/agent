"""薄协作能力层：能力发现、兼容候选选择与结果冲突收口。

本模块不执行 Prompt、RAG、Tool 或业务流程。每个被选中的 Agent 仍由自身
Release/Snapshot/Harness 运行；这里仅把“需要什么能力”稳定映射为一个已发布 Provider。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from platform_sdk.contracts.subagents import (
    AgentResult,
    CapabilityRequirement,
    ConflictStrategy,
    SubAgentBinding,
)


class CapabilityAvailability(StrEnum):
    """协作能力的可用性结论，调用方必须显式处理降级/阻断而非静默忽略。"""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class CollaborationError(RuntimeError):
    """能力目录、委派授权或冲突策略无法安全满足时抛出的失败关闭错误。"""


@dataclass(frozen=True)
class CapabilitySelection:
    """一次 Root Task 冻结的 Provider 选择，避免 Canary 或健康波动中途切换 Agent。"""

    requirement: CapabilityRequirement
    binding: SubAgentBinding
    availability: CapabilityAvailability
    reason: str


class CapabilityRouter:
    """仅根据冻结 Binding 与本地健康投影选择 Provider，热路径不查询远程 Control Plane。"""

    def select(
        self,
        bindings: Iterable[SubAgentBinding],
        requirement: CapabilityRequirement,
        *,
        caller_agent_id: str,
        unavailable_agents: frozenset[str] = frozenset(),
    ) -> CapabilitySelection:
        """选择兼容且已授权的最高权威 Provider；Required 能力无候选时明确失败。"""
        candidates = [
            item
            for item in bindings
            if requirement.capability_id.upper() in item.capabilities
            and (not item.allowed_callers or caller_agent_id in item.allowed_callers)
            and item.input_schema_version == requirement.input_schema_version
            and item.output_schema_version == requirement.output_schema_version
            and item.jurisdiction == requirement.jurisdiction
        ]
        healthy = [item for item in candidates if item.agent_id not in unavailable_agents]
        if healthy:
            selected = min(healthy, key=lambda item: (item.authority_rank, item.agent_id))
            return CapabilitySelection(requirement, selected, CapabilityAvailability.AVAILABLE, "provider_selected")
        if candidates:
            selected = min(candidates, key=lambda item: (item.authority_rank, item.agent_id))
            return CapabilitySelection(requirement, selected, CapabilityAvailability.DEGRADED, "provider_unhealthy")
        raise CollaborationError(
            f"capability unavailable: {requirement.capability_id} for {caller_agent_id}"
        )

    def select_group(
        self,
        bindings: Iterable[SubAgentBinding],
        requirement: CapabilityRequirement,
        *,
        caller_agent_id: str,
        unavailable_agents: frozenset[str] = frozenset(),
    ) -> list[CapabilitySelection]:
        """选择发布快照要求数量的独立 Provider，数量或裁决策略不接受模型输入。

        并行协作仅在同一能力、输入/输出 Schema 与管辖域均兼容时发生。缺少一个
        已声明的必需 Provider 时直接失败，而不是偷偷缩减专家数量并继续给出结论。
        """
        candidates = [
            item
            for item in bindings
            if requirement.capability_id.upper() in item.capabilities
            and (not item.allowed_callers or caller_agent_id in item.allowed_callers)
            and item.input_schema_version == requirement.input_schema_version
            and item.output_schema_version == requirement.output_schema_version
            and item.jurisdiction == requirement.jurisdiction
        ]
        if not candidates:
            raise CollaborationError(
                f"capability unavailable: {requirement.capability_id} for {caller_agent_id}"
            )
        candidates.sort(key=lambda item: (item.authority_rank, item.agent_id))
        expected_parallelism = candidates[0].parallelism
        expected_strategy = candidates[0].conflict_strategy
        if any(
            item.parallelism != expected_parallelism or item.conflict_strategy != expected_strategy
            for item in candidates
        ):
            raise CollaborationError("capability providers declare inconsistent collaboration policy")
        healthy = [item for item in candidates if item.agent_id not in unavailable_agents]
        if len(healthy) < expected_parallelism:
            raise CollaborationError(
                f"capability requires {expected_parallelism} healthy providers, found {len(healthy)}"
            )
        return [
            CapabilitySelection(
                requirement,
                item,
                CapabilityAvailability.AVAILABLE,
                "provider_group_selected",
            )
            for item in healthy[:expected_parallelism]
        ]


class ResultResolver:
    """按权威、证据与策略处理结构化结果，不以最后返回的自然语言作为最终结论。"""

    def resolve(self, results: list[AgentResult], strategy: ConflictStrategy) -> AgentResult | None:
        """一致时合并，冲突时只实施声明策略；不能可靠裁决则返回 None 交由人工处理。"""
        if not results:
            return None
        decisions = {item.decision for item in results}
        if len(decisions) == 1:
            return max(results, key=lambda item: (item.confidence, len(item.evidence_ids)))
        if strategy is ConflictStrategy.AUTHORITY:
            return min(results, key=lambda item: (item.authority_rank, -len(item.evidence_ids)))
        if strategy is ConflictStrategy.QUORUM:
            counts = {decision: sum(item.decision == decision for item in results) for decision in decisions}
            winner, count = max(counts.items(), key=lambda item: item[1])
            if count > len(results) / 2:
                return max((item for item in results if item.decision == winner), key=lambda item: item.confidence)
        # Judge/Human 不能伪装成本地模型裁决；Coordinator 返回 None 以触发已发布升级流程。
        return None


def structured_agent_result(
    result: dict, *, binding: SubAgentBinding | None, provider_agent_id: str
) -> AgentResult:
    """把子 Runtime 的公开结果收敛为可比较事实，绝不把全文答案当作仲裁依据。"""
    evidence = result.get("evidence") or []
    evidence_ids = [
        str(item.get("source_id") or item.get("id"))
        for item in evidence
        if isinstance(item, dict) and (item.get("source_id") or item.get("id"))
    ]
    status = str(result.get("status", "UNKNOWN"))
    return AgentResult(
        decision=str(result.get("decision") or result.get("termination_reason") or status),
        confidence=float(result.get("confidence", 0.0)) if status == "COMPLETED" else 0.0,
        evidence_ids=list(dict.fromkeys(evidence_ids)),
        rationale_summary=str(result.get("answer", ""))[:4_000],
        provider_agent_id=provider_agent_id,
        provider_snapshot_id=str(result.get("snapshot_id", "unknown")),
        authority_rank=binding.authority_rank if binding is not None else 10_000,
        knowledge_versions={
            str(item.get("knowledge_base")): str(item.get("index_version"))
            for item in (result.get("execution_plan", {}).get("knowledge", []) or [])
            if isinstance(item, dict) and item.get("knowledge_base")
        },
    )

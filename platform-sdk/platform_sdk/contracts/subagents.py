"""发布快照声明的受控协作契约。"""

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class CapabilityCriticality(StrEnum):
    """根任务对协作能力的依赖等级，决定不可用时能否降级而不是静默成功。"""

    REQUIRED = "required"
    OPTIONAL = "optional"
    ENHANCEMENT = "enhancement"


class ConflictStrategy(StrEnum):
    """多个 Provider 返回不一致结论时的确定性收口方式。"""

    AUTHORITY = "authority"
    QUORUM = "quorum"
    JUDGE = "judge"
    HUMAN = "human"


class CapabilityRequirement(BaseModel):
    """Planner 提出的能力需求，不包含目标 Agent、网络地址或可扩大权限的参数。"""

    capability_id: str = Field(min_length=2, max_length=160)
    criticality: CapabilityCriticality = CapabilityCriticality.REQUIRED
    input_schema_version: str = Field(default="v1", min_length=1, max_length=80)
    output_schema_version: str = Field(default="v1", min_length=1, max_length=80)
    jurisdiction: str = Field(default="global", min_length=1, max_length=80)


class AgentResult(BaseModel):
    """跨 Agent 的结构化结果，Resolver 比较证据和权威性而不比较自然语言措辞。"""

    decision: str = Field(min_length=1, max_length=80)
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    rationale_summary: str = Field(default="", max_length=4_000)
    provider_agent_id: str = Field(min_length=2, max_length=160)
    provider_snapshot_id: str = Field(min_length=1, max_length=160)
    authority_rank: int = Field(default=100, ge=1, le=10_000)
    knowledge_versions: dict[str, str] = Field(default_factory=dict)


class SubAgentBinding(BaseModel):
    """父 Agent 可委派的目标、递归深度及资源上限；未声明目标不得调用。"""

    agent_id: str = Field(min_length=2, max_length=160)
    capabilities: list[str] = Field(default_factory=list, max_length=50)
    allowed_callers: list[str] = Field(default_factory=list, max_length=100)
    delegated_permissions: list[str] = Field(default_factory=list, max_length=200)
    authority_rank: int = Field(default=100, ge=1, le=10_000)
    parallelism: int = Field(default=1, ge=1, le=5)
    conflict_strategy: ConflictStrategy = ConflictStrategy.AUTHORITY
    fallback_compatible: bool = False
    input_schema_version: str = Field(default="v1", min_length=1, max_length=80)
    output_schema_version: str = Field(default="v1", min_length=1, max_length=80)
    jurisdiction: str = Field(default="global", min_length=1, max_length=80)
    max_depth: int = Field(default=1, ge=1, le=4)
    max_budget_fraction: float = Field(default=0.25, gt=0, le=1)
    max_invocations: int = Field(default=1, ge=1, le=10)

    @field_validator("capabilities")
    @classmethod
    def _normalize_capabilities(cls, value: list[str]) -> list[str]:
        """标准化能力 ID 并拒绝重复声明，确保 Router 的候选选择可复现。"""
        normalized = [item.strip().upper() for item in value if item.strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("capabilities must be unique")
        return normalized

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolRisk(StrEnum):
    READ_ONLY = "read_only"
    WRITE_LOW_RISK = "write_low_risk"
    WRITE_HIGH_RISK = "write_high_risk"
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"


class ReleaseStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"
    ROLLED_BACK = "rolled_back"


class IssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class GraphNode(StrictModel):
    node_id: str = Field(min_length=1, max_length=100)
    kind: str = Field(min_length=1, max_length=100)
    config: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(StrictModel):
    from_node: str = Field(min_length=1, max_length=100)
    to_node: str = Field(min_length=1, max_length=100)
    condition: str | None = Field(default=None, max_length=500)


class GraphDefinition(StrictModel):
    graph_id: str = Field(min_length=1, max_length=100)
    entrypoint: str = Field(min_length=1, max_length=100)
    terminal_nodes: list[str] = Field(min_length=1)
    nodes: list[GraphNode] = Field(min_length=1)
    edges: list[GraphEdge] = Field(default_factory=list)


class PromptDefinition(StrictModel):
    prompt_id: str = Field(min_length=1, max_length=100)
    system_template: str = Field(min_length=1, max_length=100_000)
    variables: list[str] = Field(default_factory=list)
    output_schema: dict[str, Any] | None = None


class ToolBinding(StrictModel):
    tool_name: str = Field(min_length=1, max_length=150)
    version: str = Field(min_length=1, max_length=100)
    risk: ToolRisk = ToolRisk.READ_ONLY
    approval_required: bool = False
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    config: dict[str, Any] = Field(default_factory=dict)


class KnowledgeBinding(StrictModel):
    knowledge_base: str = Field(min_length=1, max_length=150)
    version: str = Field(min_length=1, max_length=100)
    filters: dict[str, Any] = Field(default_factory=dict)
    top_k: int = Field(default=8, ge=1, le=100)
    required: bool = True
    failure_mode: Literal["fail", "memory_only"] = "fail"


class ModelRoute(StrictModel):
    route_name: str = Field(min_length=1, max_length=100)
    capability: str = Field(min_length=1, max_length=100)
    models: list[str] = Field(min_length=1)
    data_region: str | None = Field(default=None, max_length=100)
    fallback_route: str | None = Field(default=None, max_length=100)


class ModelPolicy(StrictModel):
    policy_id: str = Field(min_length=1, max_length=100)
    default_route: str = Field(min_length=1, max_length=100)
    routes: list[ModelRoute] = Field(min_length=1)


class RuntimeLimits(StrictModel):
    max_steps: int = Field(default=20, ge=1, le=1_000)
    max_llm_calls: int = Field(default=12, ge=1, le=1_000)
    max_tool_calls: int = Field(default=10, ge=0, le=1_000)
    max_retrieval_rounds: int = Field(default=4, ge=0, le=100)
    max_execution_seconds: int = Field(default=300, ge=1, le=86_400)
    max_cost_usd: float = Field(default=2.0, gt=0, le=10_000)


class AgentDraftSpec(StrictModel):
    display_name: str = Field(min_length=1, max_length=150)
    description: str = Field(default="", max_length=2_000)
    graph: GraphDefinition
    prompt: PromptDefinition
    tools: list[ToolBinding] = Field(default_factory=list)
    knowledge: list[KnowledgeBinding] = Field(default_factory=list)
    model_policy: ModelPolicy
    runtime_limits: RuntimeLimits = Field(default_factory=RuntimeLimits)
    labels: dict[str, str] = Field(default_factory=dict)
    retrieval_policy: dict[str, Any] = Field(default_factory=dict)


class AgentCreate(StrictModel):
    agent_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    spec: AgentDraftSpec


class AgentDraftUpdate(StrictModel):
    expected_revision: int = Field(ge=1)
    spec: AgentDraftSpec


class AgentDefinition(StrictModel):
    tenant_id: str
    agent_id: str
    revision: int
    draft: AgentDraftSpec
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime


class TenantPolicy(StrictModel):
    tenant_id: str
    allowed_models: list[str] = Field(default_factory=list)
    allowed_data_regions: list[str] = Field(default_factory=list)
    max_canary_percentage: int = Field(default=100, ge=0, le=100)
    require_approval_for_high_risk_tools: bool = True
    updated_by: str = "system"
    updated_at: datetime = Field(default_factory=utc_now)


class TenantPolicyUpdate(StrictModel):
    allowed_models: list[str] = Field(default_factory=list)
    allowed_data_regions: list[str] = Field(default_factory=list)
    max_canary_percentage: int = Field(default=100, ge=0, le=100)
    require_approval_for_high_risk_tools: bool = True


class ValidationIssue(StrictModel):
    severity: IssueSeverity
    code: str
    path: str
    message: str


class ValidationReport(StrictModel):
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class PublishedSnapshot(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    tenant_id: str
    agent_id: str
    agent_version: str
    graph_version: str
    prompt_version: str
    knowledge_version: str
    tool_set_version: str
    model_policy_version: str
    spec: AgentDraftSpec
    published_at: datetime


class AgentVersionPublish(StrictModel):
    semantic_version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
    change_summary: str = Field(default="", max_length=2_000)


class AgentVersion(StrictModel):
    tenant_id: str
    version_id: str
    agent_id: str
    semantic_version: str
    source_revision: int
    content_hash: str
    snapshot: PublishedSnapshot
    change_summary: str
    published_by: str
    published_at: datetime


class ReleaseCreate(StrictModel):
    version_id: str
    environment: str = Field(default="production", pattern=r"^[a-z][a-z0-9-]{1,31}$")
    rollout_percentage: int = Field(default=100, ge=0, le=100)
    tenant_allowlist: list[str] = Field(default_factory=list)
    reason: str = Field(default="", max_length=2_000)
    quality_gate_run_id: str | None = Field(default=None, max_length=160)


class ReleasePromote(StrictModel):
    rollout_percentage: int = Field(ge=1, le=100)


class ReleaseManifest(StrictModel):
    tenant_id: str
    release_id: str
    agent_id: str
    version_id: str
    environment: str
    rollout_percentage: int
    tenant_allowlist: list[str] = Field(default_factory=list)
    status: ReleaseStatus
    previous_release_id: str | None = None
    reason: str
    quality_gate_id: str | None = None
    quality_gate_metrics: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: datetime
    updated_at: datetime


class RuntimeResolution(StrictModel):
    tenant_id: str
    agent_id: str
    environment: str
    session_id: str
    release_id: str
    version_id: str
    assignment: Literal["stable", "canary", "allowlist", "pinned", "first_release"]
    pinned: bool
    snapshot: PublishedSnapshot


class OutboxEvent(StrictModel):
    event_id: str
    event_type: str
    trace_id: str
    tenant_id: str
    aggregate_type: str
    aggregate_id: str
    schema_version: Literal["1.0"] = "1.0"
    occurred_at: datetime
    payload: dict[str, Any]
    published_at: datetime | None = None


class OutboxList(StrictModel):
    items: list[OutboxEvent]
    next_cursor: int | None = None


class HealthStatus(StrictModel):
    status: Literal["ok"]
    service: Literal["agent-control-plane"] = "agent-control-plane"


class ErrorResponse(StrictModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class Identity(StrictModel):
    tenant_id: str
    user_id: str
    roles: frozenset[str] = frozenset()

    @field_validator("roles", mode="before")
    @classmethod
    def normalize_roles(cls, value: Any) -> frozenset[str]:
        if value is None:
            return frozenset()
        if isinstance(value, str):
            return frozenset(part.strip() for part in value.split(",") if part.strip())
        return frozenset(value)

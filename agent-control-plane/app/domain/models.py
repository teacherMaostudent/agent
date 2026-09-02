from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from platform_sdk.contracts.execution_profile import (
    ContextStrategy,
    DurabilityStrategy,
    ExecutionEngine,
    PlanningStrategy,
    StepExecutionStrategy,
    ToolPresentationMode,
)
from platform_sdk.contracts.orchestration import ReasoningPolicy
from platform_sdk.contracts.skills import (
    CapabilityProviderDescriptor,
    CapabilityRoutingPolicy,
    CompiledSkillPlan,
    SkillBinding,
    SkillSpec,
)
from platform_sdk.contracts.subagents import SubAgentBinding
from platform_sdk.contracts.workflow import CompiledWorkflowPlan, WorkflowSpec
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    """返回统一 UTC 时间，确保版本、发布和 Outbox 事件可跨区域正确排序。"""
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolRisk(StrEnum):
    READ_ONLY = "read_only"
    WRITE_LOW_RISK = "write_low_risk"
    WRITE_HIGH_RISK = "write_high_risk"
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"


class ToolVersionStatus(StrEnum):
    """冻结工具版本的生命周期；只有 PUBLISHED 版本能进入 Gateway 投影。"""

    CANDIDATE = "candidate"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


class ReleaseStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"
    ROLLED_BACK = "rolled_back"


class ReleaseStage(StrEnum):
    """发布投影的风险暴露阶段；它不描述 Agent Version 的草稿/编译生命周期。"""

    SHADOW = "shadow"
    CANARY = "canary"
    PRODUCTION = "production"


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
    side_effect: bool = False
    idempotent: bool = False
    required_permissions: list[str] = Field(default_factory=list, max_length=200)
    # Catalog output contract frozen in the Snapshot; Runtime never reads a live catalog.
    output_schema: dict[str, Any] | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class ToolDraftCreate(StrictModel):
    """创建可变工具资产草稿；definition 必须符合平台 Tool Catalog Schema。"""

    tool_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,149}$")
    definition: dict[str, Any]
    owner_team: str = Field(default="platform", min_length=1, max_length=120)
    change_summary: str = Field(default="", max_length=2_000)


class ToolDraftUpdate(StrictModel):
    """以 revision CAS 更新工具草稿，避免并发编辑覆盖安全契约。"""

    expected_revision: int = Field(ge=1)
    definition: dict[str, Any]
    change_summary: str = Field(default="", max_length=2_000)


class ToolDefinition(StrictModel):
    """租户范围的工具管理面聚合；运行时绝不直接使用此可变对象。"""

    tenant_id: str
    tool_id: str
    revision: int
    definition: dict[str, Any]
    owner_team: str
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime


class ToolVersionPublish(StrictModel):
    """冻结工具草稿为不可变候选版本，等待独立审核。"""

    semantic_version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
    change_summary: str = Field(default="", max_length=2_000)


class ToolVersion(StrictModel):
    """工具不可变版本；runtime_definition 是 Gateway 唯一可执行契约。"""

    tenant_id: str
    version_id: str
    tool_id: str
    semantic_version: str
    source_revision: int
    content_sha256: str
    runtime_definition: dict[str, Any]
    status: ToolVersionStatus = ToolVersionStatus.CANDIDATE
    change_summary: str
    published_by: str
    published_at: datetime
    updated_at: datetime


class ToolReviewCreate(StrictModel):
    """审核候选版本；批准和拒绝都作为不可变审计记录保留。"""

    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=4_000)


class ToolVersionStatusUpdate(StrictModel):
    """用于已发布版本的弃用/退役，不允许重写版本内容。"""

    status: Literal["deprecated", "retired"]
    reason: str = Field(default="", max_length=2_000)


class KnowledgeBinding(StrictModel):
    knowledge_base: str = Field(min_length=1, max_length=150)
    version: str = Field(min_length=1, max_length=100)
    filters: dict[str, Any] = Field(default_factory=dict)
    top_k: int = Field(default=8, ge=1, le=100)
    required: bool = True
    failure_mode: Literal["fail", "memory_only"] = "fail"
    # 发布态必须指向一个可比较的索引空间; 空值仅兼容旧快照, 生产校验会拒绝它。
    index_version: str = Field(default="", max_length=160)
    embedding_contract_id: str = Field(default="", max_length=80)
    retrieval_evaluation_id: str = Field(default="", max_length=160)


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


class ExecutionRequirements(StrictModel):
    """冻结宏观规划、执行内核、持久化和上下文等正交运行维度。"""

    lifecycle: Literal["request_scoped", "durable_workflow"] = "request_scoped"
    reasoning: Literal["minimal", "agentic", "graph"] = "graph"
    engine: ExecutionEngine | None = None
    durability: DurabilityStrategy | None = None
    planning_strategy: PlanningStrategy = PlanningStrategy.PLAN_EXECUTE
    default_step_strategy: StepExecutionStrategy = StepExecutionStrategy.DETERMINISTIC
    adaptive_step_strategy: StepExecutionStrategy = StepExecutionStrategy.REACT
    context_strategy: ContextStrategy = ContextStrategy.MANAGED_LEDGER
    tool_presentation: ToolPresentationMode = ToolPresentationMode.NATIVE

    @model_validator(mode="after")
    def normalize_execution_projection(self) -> ExecutionRequirements:
        """以新执行维度生成兼容字段，并拒绝一个声明表达两套相反语义。"""
        if self.engine is not None:
            expected_reasoning = {
                ExecutionEngine.SIMPLE: "minimal",
                ExecutionEngine.DEEP_AGENTS: "agentic",
                ExecutionEngine.LANGGRAPH: "graph",
            }[self.engine]
            if "reasoning" in self.model_fields_set and self.reasoning != expected_reasoning:
                raise ValueError("reasoning conflicts with execution engine")
            self.reasoning = expected_reasoning
        if self.durability is not None:
            expected_lifecycle = (
                "durable_workflow"
                if self.durability == DurabilityStrategy.TEMPORAL
                else "request_scoped"
            )
            if "lifecycle" in self.model_fields_set and self.lifecycle != expected_lifecycle:
                raise ValueError("lifecycle conflicts with durability strategy")
            self.lifecycle = expected_lifecycle
        return self


class IntentDefinition(StrictModel):
    """一条可随 Agent 发布的确定性意图规则，模型只能在该规则之后补充语义判断。"""

    name: str = Field(min_length=1, max_length=100)
    domain: str = Field(min_length=1, max_length=100)
    action: str = Field(min_length=1, max_length=100)
    examples: list[str] = Field(min_length=1, max_length=100)
    required_entities: list[str] = Field(default_factory=list, max_length=50)


class IntentCatalogBinding(StrictModel):
    """Snapshot 内嵌的意图目录；嵌入内容避免 Runtime 依赖一份会漂移的全局规则表。"""

    version: str = Field(min_length=1, max_length=160)
    definitions: list[IntentDefinition] = Field(min_length=1, max_length=500)


class AgentDraftSpec(StrictModel):
    display_name: str = Field(min_length=1, max_length=150)
    description: str = Field(default="", max_length=2_000)
    graph: GraphDefinition
    prompt: PromptDefinition
    tools: list[ToolBinding] = Field(default_factory=list)
    knowledge: list[KnowledgeBinding] = Field(default_factory=list)
    model_policy: ModelPolicy
    runtime_limits: RuntimeLimits = Field(default_factory=RuntimeLimits)
    runtime_executor: str = Field(default="declarative-langgraph/v1", min_length=1, max_length=100)
    execution: ExecutionRequirements | None = None
    intent_catalog_version: str = Field(default="platform-default/v1", min_length=1, max_length=160)
    intent_catalog: IntentCatalogBinding | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    retrieval_policy: dict[str, Any] = Field(default_factory=dict)
    subagents: list[SubAgentBinding] = Field(default_factory=list)
    # 引用的是 Skill Registry 已发布版本的摘要, 而非可在运行时变动的名字或 URL。
    skills: list[SkillBinding] = Field(default_factory=list)
    # Planner 只输出能力需求, 候选 Provider 和顺序与 Snapshot 一起冻结。
    capability_providers: list[CapabilityProviderDescriptor] = Field(default_factory=list)
    capability_routing: list[CapabilityRoutingPolicy] = Field(default_factory=list)
    reasoning_policy: ReasoningPolicy = Field(default_factory=ReasoningPolicy)

    @model_validator(mode="after")
    def _validate_intent_catalog_binding(self) -> AgentDraftSpec:
        """保证声明版本和内嵌目录一致，禁止发布时把 A 版本号绑定到 B 的规则正文。"""
        if self.intent_catalog and self.intent_catalog.version != self.intent_catalog_version:
            raise ValueError("intent_catalog.version must equal intent_catalog_version")
        return self


class AgentCreate(StrictModel):
    agent_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    spec: AgentDraftSpec


class SkillStatus(StrEnum):
    """不可变 SkillVersion 的可用状态；草稿本身不进入该状态机。"""

    VALIDATING = "validating"
    CANDIDATE = "candidate"
    CANARY = "canary"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETIRED = "retired"
    DEGRADED = "degraded"
    QUARANTINED = "quarantined"


class SkillCreate(StrictModel):
    """创建可编辑 Skill 草稿；Skill 的运行工件只能通过后续发布生成。"""

    skill_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    spec: SkillSpec

    @model_validator(mode="after")
    def match_skill_identity(self) -> SkillCreate:
        """保证请求 ID 与草稿内容一致，避免跨 Skill 覆盖。"""
        if self.skill_id != self.spec.skill_id:
            raise ValueError("skill_id must equal spec.skill_id")
        return self


class SkillDraftUpdate(StrictModel):
    """带乐观锁修订号的 Skill 草稿更新请求。"""

    expected_revision: int = Field(ge=1)
    spec: SkillSpec


class SkillDefinition(StrictModel):
    """租户隔离的可编辑 Skill 草稿聚合。"""

    tenant_id: str
    skill_id: str
    revision: int
    draft: SkillSpec
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime


class SkillVersionPublish(StrictModel):
    """将 Skill 草稿冻结为精确版本的发布命令。"""

    semantic_version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
    change_summary: str = Field(default="", max_length=2_000)


class SkillVersion(StrictModel):
    """已编译 Skill 工件；Runtime 只接受其摘要一致的精确引用。"""

    tenant_id: str
    version_id: str
    skill_id: str
    semantic_version: str
    source_revision: int
    artifact_digest: str
    plan: CompiledSkillPlan
    status: SkillStatus = SkillStatus.VALIDATING
    change_summary: str
    published_by: str
    published_at: datetime
    updated_at: datetime


class SkillRuntimeResolution(StrictModel):
    """Runtime/Agent Lab 可读取的 Active SkillVersion 冻结工件。"""

    tenant_id: str
    skill_id: str
    version: str
    artifact_digest: str
    plan: CompiledSkillPlan


class SkillStatusUpdate(StrictModel):
    """治理或运维发起的受控状态切换；不允许修改冻结工件。"""

    status: SkillStatus
    quality_gate_run_id: str | None = Field(default=None, max_length=160)


class WorkflowCreate(StrictModel):
    """创建 Workflow Draft；该对象与 Agent Draft 分离，不能借用 Agent 发布链。"""

    workflow_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    spec: WorkflowSpec


class WorkflowDefinition(StrictModel):
    """租户隔离的可编辑 Workflow Draft。"""

    tenant_id: str
    workflow_id: str
    revision: int
    draft: WorkflowSpec
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime


class WorkflowVersionPublish(StrictModel):
    """将 Workflow Draft 固化为零 Agent 运行时工件。"""

    semantic_version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


class WorkflowVersion(StrictModel):
    """不可变 WorkflowVersion；Runtime 仅加载其中的已编译计划。"""

    tenant_id: str
    version_id: str
    workflow_id: str
    semantic_version: str
    source_revision: int
    artifact_digest: str
    plan: CompiledWorkflowPlan
    published_by: str
    published_at: datetime


class WorkflowDraftUpdate(StrictModel):
    """通过 CAS 更新 Workflow Draft，禁止并发覆盖。"""

    expected_revision: int = Field(ge=1)
    spec: WorkflowSpec


class WorkflowReleaseCreate(StrictModel):
    """把一个冻结 WorkflowVersion 激活到目标环境。"""

    version_id: str = Field(min_length=1, max_length=160)
    environment: str = Field(default="production", pattern=r"^[a-z][a-z0-9-]{1,31}$")


class WorkflowRelease(StrictModel):
    """Runtime 可解析的 Workflow 发布记录；同环境仅一个 Active 版本。"""

    tenant_id: str
    release_id: str
    workflow_id: str
    version_id: str
    environment: str
    status: Literal["active", "retired"] = "active"
    created_by: str
    created_at: datetime


class WorkflowRuntimeResolution(StrictModel):
    """Control Plane 返回给 Runtime 的冻结零 Agent Workflow 工件。"""

    tenant_id: str
    workflow_id: str
    environment: str
    release_id: str
    version_id: str
    plan: CompiledWorkflowPlan
    artifact_digest: str


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


class LlmQuotaLimit(StrictModel):
    """Daily tenant/user budget compiled by Control Plane and enforced by LLM Gateway."""

    daily_token_limit: int = Field(default=50_000, ge=1, le=10_000_000_000)
    daily_cost_limit_usd: float = Field(default=1.0, gt=0, le=10_000_000)
    currency: Literal["USD"] = "USD"


class TenantStatus(StrEnum):
    """租户生命周期状态；不提供物理删除，避免破坏既有运行与审计证据。"""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    RETIRED = "retired"


class Tenant(StrictModel):
    """平台级租户目录记录，不与任何人的 ``user_id`` 或登录名混用。

    ``tenant_id`` 是不可变的机器标识；展示名称可以修改。所有业务服务继续以该
    标识隔离数据、策略与审计，而不是以人类账号名作为隔离键。
    """

    tenant_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")
    display_name: str = Field(min_length=1, max_length=120)
    status: TenantStatus = TenantStatus.ACTIVE
    data_region: str = Field(default="local", min_length=1, max_length=64)
    created_by: str = Field(min_length=1, max_length=255)
    created_at: datetime = Field(default_factory=utc_now)
    updated_by: str = Field(min_length=1, max_length=255)
    updated_at: datetime = Field(default_factory=utc_now)


class TenantCreate(StrictModel):
    """最高管理员创建新租户时提交的受限字段。"""

    tenant_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")
    display_name: str = Field(min_length=1, max_length=120)
    data_region: str = Field(default="local", min_length=1, max_length=64)


class TenantUpdate(StrictModel):
    """更新租户元数据或生命周期状态；tenant_id 永远不能在原地改名。"""

    display_name: str = Field(min_length=1, max_length=120)
    data_region: str = Field(min_length=1, max_length=64)
    status: TenantStatus
    reason: str = Field(min_length=3, max_length=1_000)


class TenantPolicy(StrictModel):
    tenant_id: str
    allowed_models: list[str] = Field(default_factory=list)
    allowed_data_regions: list[str] = Field(default_factory=list)
    max_canary_percentage: int = Field(default=100, ge=0, le=100)
    require_approval_for_high_risk_tools: bool = True
    llm_quotas: dict[str, LlmQuotaLimit] = Field(default_factory=dict, max_length=1_000)
    updated_by: str = "system"
    updated_at: datetime = Field(default_factory=utc_now)


class TenantPolicyUpdate(StrictModel):
    allowed_models: list[str] = Field(default_factory=list)
    allowed_data_regions: list[str] = Field(default_factory=list)
    max_canary_percentage: int = Field(default=100, ge=0, le=100)
    require_approval_for_high_risk_tools: bool = True
    llm_quotas: dict[str, LlmQuotaLimit] = Field(default_factory=dict, max_length=1_000)


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
    runtime_artifact: dict[str, Any] | None = None


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
    agent_lab_experiment_id: str | None = Field(default=None, max_length=160)


class ReleaseProjection(StrictModel):
    """Runtime 只能消费的、由 Control Plane 发布的风险执行上下文。

    Snapshot 固定 Agent 定义；Projection 固定本次 Release 如何暴露流量和处理副作用。
    两者在创建 Run 时一起钉扎，恢复或重试不得重新按当前线上状态推导。
    """

    release_stage: ReleaseStage = ReleaseStage.PRODUCTION
    traffic_policy_version: str = Field(default="traffic-policy/v1", min_length=1, max_length=160)
    side_effect_policy_version: str = Field(
        default="side-effect-policy/v1", min_length=1, max_length=160
    )
    # 只保存策略引用和不可敏感的执行规则。密钥、真实业务正文或模拟脚本不进入 Release。
    side_effect_policy: dict[str, Any] = Field(default_factory=dict)
    # 流量规则是发布投影的一部分，而不是由 Runtime 按当前配置临时拼接。当前只有
    # IdP 角色能够作为可信选择信号；请求类型/业务风险必须先由受信分类服务签名后才可
    # 扩展进来，避免浏览器用 metadata 把自己“升级”为灰度用户。
    traffic_policy: dict[str, Any] = Field(default_factory=dict)
    shadow_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    shadow_resource_budget: dict[str, Any] = Field(default_factory=dict)
    revision: int = Field(default=1, ge=1)


class ReleasePromote(StrictModel):
    rollout_percentage: int = Field(ge=1, le=100)


class ReleaseStartShadow(StrictModel):
    """开启只供内部镜像流量使用的 Shadow 投影，不向真实用户返回候选结果。"""

    shadow_sample_rate: float = Field(gt=0.0, le=1.0)
    side_effect_policy_version: str = Field(min_length=1, max_length=160)
    side_effect_policy: dict[str, Any] = Field(default_factory=dict)
    shadow_resource_budget: dict[str, Any] = Field(default_factory=dict)


class ReleaseStartCanary(StrictModel):
    """通过已持久化 Shadow Gate 后才允许开始真实用户灰度。"""

    rollout_percentage: int = Field(ge=1, le=100)
    decision_id: str = Field(min_length=1, max_length=160)
    traffic_policy_version: str = Field(default="traffic-policy/v1", min_length=1, max_length=160)
    # 只允许用 IdP 已验证角色缩小 Canary 人群。空数组代表不做角色限制；它不会授予
    # 权限，最终仍由 Runtime/Tool Gateway 的权限与审批链决定可执行能力。
    eligible_roles: list[str] = Field(default_factory=list, max_length=100)
    excluded_roles: list[str] = Field(default_factory=list, max_length=100)


class GovernanceReleaseAction(StrictModel):
    """Control Plane 消费已保存 GateDecision 的受控动作请求。"""

    decision_id: str = Field(min_length=1, max_length=160)
    promote_to_percentage: int | None = Field(default=None, ge=1, le=100)


class ReleaseManifest(StrictModel):
    tenant_id: str
    release_id: str
    agent_id: str
    version_id: str
    environment: str
    rollout_percentage: int
    tenant_allowlist: list[str] = Field(default_factory=list)
    projection: ReleaseProjection = Field(default_factory=ReleaseProjection)
    status: ReleaseStatus
    previous_release_id: str | None = None
    reason: str
    quality_gate_id: str | None = None
    quality_gate_metrics: dict[str, Any] = Field(default_factory=dict)
    agent_lab_experiment_id: str | None = None
    runtime_executor_catalog_version: str | None = None
    runtime_executor_cluster_id: str | None = None
    runtime_executor_catalog_hash: str | None = None
    runtime_capability_manifest_digest: str | None = None
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
    assignment: Literal["stable", "canary", "allowlist", "pinned", "first_release", "shadow"]
    pinned: bool
    snapshot: PublishedSnapshot
    # The complete projection is returned rather than letting Runtime infer a stage from percent.
    release_projection: ReleaseProjection = Field(default_factory=ReleaseProjection)
    shadow_sampled: bool = False


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
        """兼容 OIDC 声明与旧 Header 字符串，并归一为不可变权限集合。"""
        if value is None:
            return frozenset()
        if isinstance(value, str):
            return frozenset(part.strip() for part in value.split(",") if part.strip())
        return frozenset(value)

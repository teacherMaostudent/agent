"""Skill、业务能力与顶层编排边界的跨服务契约。

本模块刻意把三种概念分开：``RuntimeCapability`` 表示一个 Runtime 实例是否部署了
某项基础设施；这里的 ``Capability`` 表示业务任务需要的专业能力；``Skill`` 则是
能够提供该业务能力的、版本冻结的声明式执行单元。这样 Skill 不会被误当成任意插件，
也不会与独立责任主体 Agent 混为一谈。
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field, field_validator, model_validator


class OrchestrationOwner(StrEnum):
    """RootTask 唯一的下一步决策所有者。"""

    WORKFLOW = "workflow"
    AGENT = "agent"


class ExecutionTopology(StrEnum):
    """Agent 所采用的组织拓扑；它不是另一种顶层编排模式。"""

    NONE = "none"
    SINGLE_AGENT = "single_agent"
    SUB_AGENT = "sub_agent"
    MULTI_AGENT = "multi_agent"


class CapabilityProviderKind(StrEnum):
    """可提供业务能力的受控主体类型。"""

    TOOL = "tool"
    SKILL = "skill"
    AGENT = "agent"
    HUMAN = "human"
    RAG = "rag"
    MEMORY = "memory"
    WORKFLOW = "workflow"


class ProviderHealthStatus(StrEnum):
    """Capability Provider 的统一可用状态。"""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    QUARANTINED = "quarantined"


class SkillActivationPolicy(StrEnum):
    """Skill 允许被选择的入口范围；默认必须由显式绑定调用。"""

    EXPLICIT_ONLY = "explicit_only"
    CAPABILITY_RESOLVER = "capability_resolver"
    WORKFLOW_ONLY = "workflow_only"


class SkillRiskLevel(StrEnum):
    """Skill 自身的风险提示，不能覆盖其内部 Tool 的更高风险声明。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SkillGovernanceProfile(StrEnum):
    """按副作用特征复用治理要求，避免每个 Skill 重复声明整套安全字段。"""

    PURE = "pure"
    READ_ONLY = "read_only"
    REVERSIBLE_WRITE = "reversible_write"
    HIGH_RISK_WRITE = "high_risk_write"
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"


class SkillGovernanceControls(BaseModel):
    """仅写入型 Skill 需要的可选前后置、验证与补偿引用。"""

    precondition_ids: list[str] = Field(default_factory=list, max_length=50)
    postcondition_ids: list[str] = Field(default_factory=list, max_length=50)
    verifier_id: str = Field(default="", max_length=160)
    compensation_skill_id: str = Field(default="", max_length=160)
    evidence_required: bool = False


class CapabilityDeclaration(BaseModel):
    """Skill 或其他 Provider 可满足的一项版本化业务能力。"""

    capability_id: str = Field(min_length=2, max_length=160)
    input_schema_version: str = Field(default="v1", min_length=1, max_length=80)
    output_schema_version: str = Field(default="v1", min_length=1, max_length=80)

    @field_validator("capability_id")
    @classmethod
    def normalize_capability_id(cls, value: str) -> str:
        """规范化能力 ID，保证 Resolver 的匹配不依赖调用方大小写。"""
        return value.strip().upper()


class SkillInstructionBinding(BaseModel):
    """冻结的 Prompt 引用及正文；Runtime 只读取快照正文，绝不拉取最新 Prompt。"""

    prompt_id: str = Field(min_length=1, max_length=160)
    prompt_version: str = Field(min_length=1, max_length=100)
    system_template: str = Field(min_length=1, max_length=100_000)
    variables: list[str] = Field(default_factory=list, max_length=100)


class SkillToolBinding(BaseModel):
    """Skill 可提出的工具意图；实际鉴权、审批和副作用仍在 Tool Gateway。"""

    tool_name: str = Field(min_length=1, max_length=150)
    version: str = Field(min_length=1, max_length=100)
    required_permissions: list[str] = Field(default_factory=list, max_length=200)


class SkillKnowledgeBinding(BaseModel):
    """Skill 固定读取的知识空间；不允许绕过 RAG 的 ACL 与索引版本。"""

    knowledge_base: str = Field(min_length=1, max_length=150)
    version: str = Field(min_length=1, max_length=100)
    index_version: str = Field(min_length=1, max_length=160)
    embedding_contract_id: str = Field(min_length=1, max_length=100)
    filters: dict[str, Any] = Field(default_factory=dict)


class SkillCompositionPolicy(BaseModel):
    """限制 Skill 组合深度和并发，防止声明式能力组合膨胀成隐式 Agent Loop。"""

    max_active_skills: int = Field(default=3, ge=1, le=20)
    max_skill_depth: int = Field(default=1, ge=0, le=4)
    max_invocations: int = Field(default=1, ge=1, le=50)
    max_budget_fraction: float = Field(default=0.25, gt=0, le=1)
    allowed_dependencies: list[str] = Field(default_factory=list, max_length=50)
    conflicts_with: list[str] = Field(default_factory=list, max_length=50)
    priority: int = Field(default=100, ge=1, le=10_000)
    estimated_context_tokens: int = Field(default=0, ge=0, le=1_000_000)
    max_total_context_tokens: int = Field(default=32_000, ge=1, le=2_000_000)
    allow_tool_overlap: bool = True


class SkillQualificationBinding(BaseModel):
    """Skill 进入候选/正式状态必须引用的治理评测基线。"""

    evaluation_dataset_id: str = Field(min_length=1, max_length=160)
    qualification_policy_id: str = Field(min_length=1, max_length=160)


class SkillSpec(BaseModel):
    """可发布 Skill 的声明性定义，不含动态 Python、HTTP 地址或未固定版本的依赖。"""

    skill_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    description: str = Field(default="", max_length=2_000)
    provides: list[CapabilityDeclaration] = Field(min_length=1, max_length=50)
    activation: SkillActivationPolicy = SkillActivationPolicy.EXPLICIT_ONLY
    instructions: SkillInstructionBinding
    tools: list[SkillToolBinding] = Field(default_factory=list, max_length=100)
    knowledge: list[SkillKnowledgeBinding] = Field(default_factory=list, max_length=100)
    retrieval_policy: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    risk: SkillRiskLevel = SkillRiskLevel.LOW
    governance_profile: SkillGovernanceProfile | None = None
    governance_controls: SkillGovernanceControls = Field(
        default_factory=SkillGovernanceControls
    )
    composition: SkillCompositionPolicy = Field(default_factory=SkillCompositionPolicy)
    qualification: SkillQualificationBinding
    logical_model: str = Field(default="skill-default", min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_frozen_contract(self) -> SkillSpec:
        """拒绝重复能力/依赖与无效 Schema，确保编译产物可独立、确定地解释。"""
        capability_ids = [item.capability_id for item in self.provides]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("skill.provides must not contain duplicate capabilities")
        tool_keys = [(item.tool_name, item.version) for item in self.tools]
        if len(tool_keys) != len(set(tool_keys)):
            raise ValueError(
                "skill.tools must not contain duplicate name/version bindings"
            )
        knowledge_keys = [
            (item.knowledge_base, item.version) for item in self.knowledge
        ]
        if len(knowledge_keys) != len(set(knowledge_keys)):
            raise ValueError(
                "skill.knowledge must not contain duplicate name/version bindings"
            )
        if self.skill_id in self.composition.allowed_dependencies:
            raise ValueError("a skill cannot declare itself as a dependency")
        if self.skill_id in self.composition.conflicts_with:
            raise ValueError("a skill cannot conflict with itself")
        Draft202012Validator.check_schema(self.input_schema or {"type": "object"})
        Draft202012Validator.check_schema(self.output_schema or {"type": "object"})
        profile = self.resolved_governance_profile()
        if profile == SkillGovernanceProfile.PURE and self.tools:
            raise ValueError("PURE skills cannot bind tools")
        if (
            self.governance_profile
            in {
                SkillGovernanceProfile.HIGH_RISK_WRITE,
                SkillGovernanceProfile.HUMAN_APPROVAL_REQUIRED,
            }
            and not self.governance_controls.verifier_id
        ):
            raise ValueError("high-risk skills must bind a deterministic verifier")
        if (
            self.governance_profile == SkillGovernanceProfile.REVERSIBLE_WRITE
            and not self.governance_controls.compensation_skill_id
        ):
            raise ValueError("reversible-write skills must bind a compensation skill")
        return self

    def resolved_governance_profile(self) -> SkillGovernanceProfile:
        """为旧 Skill 推导保守 Profile；新发布应显式冻结该字段。"""
        if self.governance_profile is not None:
            return self.governance_profile
        if not self.tools:
            return SkillGovernanceProfile.PURE
        if self.risk == SkillRiskLevel.HIGH:
            return SkillGovernanceProfile.HIGH_RISK_WRITE
        return SkillGovernanceProfile.READ_ONLY


class SkillBinding(BaseModel):
    """Agent 或 Workflow 对一个精确 SkillVersion 的不可变引用。"""

    skill_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    version: str = Field(min_length=1, max_length=100)
    artifact_digest: str = Field(min_length=32, max_length=128)
    required: bool = False
    max_invocations: int = Field(default=1, ge=1, le=50)
    max_budget_fraction: float = Field(default=0.25, gt=0, le=1)


class CompiledSkillPlan(BaseModel):
    """发布期生成的 Skill 执行计划；可被 Runtime 加载但不能被请求覆写。"""

    contract_version: str = "skill-plan/v1"
    contract_hash: str = Field(min_length=32, max_length=128)
    skill_id: str
    version: str
    artifact_digest: str
    description: str = ""
    provides: list[CapabilityDeclaration]
    activation: SkillActivationPolicy
    instructions: SkillInstructionBinding
    tools: list[SkillToolBinding] = Field(default_factory=list)
    knowledge: list[SkillKnowledgeBinding] = Field(default_factory=list)
    retrieval_policy: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    risk: SkillRiskLevel
    governance_profile: SkillGovernanceProfile = SkillGovernanceProfile.PURE
    governance_controls: SkillGovernanceControls = Field(
        default_factory=SkillGovernanceControls
    )
    composition: SkillCompositionPolicy
    qualification: SkillQualificationBinding
    logical_model: str = "skill-default"


class CapabilityProviderDescriptor(BaseModel):
    """发布快照中的业务能力 Provider 声明，不包含可由模型覆写的网络位置。"""

    provider_id: str = Field(min_length=2, max_length=160)
    kind: CapabilityProviderKind
    capabilities: list[CapabilityDeclaration] = Field(min_length=1, max_length=50)
    version: str = Field(min_length=1, max_length=100)
    artifact_digest: str = Field(default="", max_length=128)
    rag_index_version: str = Field(default="", max_length=160)
    embedding_contract_id: str = Field(default="", max_length=100)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    requires_independent_authority: bool = False
    requires_long_running: bool = False
    fallback_safe: bool = False
    qualified: bool = True
    healthy: bool = True
    priority: int = Field(default=100, ge=1, le=10_000)
    required_permissions: list[str] = Field(default_factory=list, max_length=200)
    max_cost_usd: float = Field(default=0.0, ge=0)
    max_latency_ms: int = Field(default=0, ge=0)
    health_status: ProviderHealthStatus = ProviderHealthStatus.AVAILABLE

    @model_validator(mode="after")
    def validate_provider_contract(self) -> CapabilityProviderDescriptor:
        """验证 Provider 工件和 Schema，防止 Resolver 选中无法执行的声明。"""
        Draft202012Validator.check_schema(self.input_schema or {"type": "object"})
        Draft202012Validator.check_schema(self.output_schema or {"type": "object"})
        if self.kind == CapabilityProviderKind.RAG and (
            not self.rag_index_version or not self.embedding_contract_id
        ):
            raise ValueError(
                "RAG provider requires rag_index_version and embedding_contract_id"
            )
        return self

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(
        cls, value: list[CapabilityDeclaration]
    ) -> list[CapabilityDeclaration]:
        """拒绝同一 Provider 重复声明能力，避免优先级排序受到配置重复影响。"""
        if len({item.capability_id for item in value}) != len(value):
            raise ValueError("provider capabilities must be unique")
        return value


class CapabilityRoutingPolicy(BaseModel):
    """每项能力独立声明 Provider 顺序，避免固化 Tool→Skill→Agent→Human。"""

    capability_id: str = Field(min_length=2, max_length=160)
    provider_order: list[str] = Field(min_length=1, max_length=100)
    allow_degraded: bool = False

    @field_validator("capability_id")
    @classmethod
    def normalize_policy_capability(cls, value: str) -> str:
        """规范化策略能力 ID。"""
        return value.strip().upper()


class CapabilityResolutionRequest(BaseModel):
    """Planner 输出的能力约束；禁止携带目标 Provider 或可扩大权限的字段。"""

    capability_id: str = Field(min_length=2, max_length=160)
    caller_permissions: frozenset[str] = frozenset()
    max_cost_usd: float = Field(default=0.0, ge=0)
    max_latency_ms: int = Field(default=0, ge=0)
    require_independent_authority: bool = False

    @field_validator("capability_id")
    @classmethod
    def normalize_requested_capability(cls, value: str) -> str:
        """让 Planner 和快照的能力匹配采用同一规范化键。"""
        return value.strip().upper()


class SkillCard(BaseModel):
    """渐进披露阶段可见的最小 Skill 摘要，不包含 Prompt、示例或工具参数。"""

    skill_id: str
    version: str
    description: str
    provides: list[str]
    risk: SkillRiskLevel


def compile_skill_plan(
    spec: SkillSpec, *, version: str, artifact_digest: str = ""
) -> CompiledSkillPlan:
    """将一个已校验的 Skill 定义冻结为可审计计划，而不是让 Runtime 解释草稿。"""
    if not version.strip():
        raise ValueError("skill version must not be empty")
    payload = {
        "contract_version": "skill-plan/v1",
        "skill_id": spec.skill_id,
        "version": version,
        "description": spec.description,
        "provides": [item.model_dump(mode="json") for item in spec.provides],
        "activation": spec.activation.value,
        "instructions": spec.instructions.model_dump(mode="json"),
        "tools": [item.model_dump(mode="json") for item in spec.tools],
        "knowledge": [item.model_dump(mode="json") for item in spec.knowledge],
        "retrieval_policy": spec.retrieval_policy,
        "input_schema": spec.input_schema,
        "output_schema": spec.output_schema,
        "risk": spec.risk.value,
        "governance_profile": spec.resolved_governance_profile().value,
        "governance_controls": spec.governance_controls.model_dump(mode="json"),
        "composition": spec.composition.model_dump(mode="json"),
        "qualification": spec.qualification.model_dump(mode="json"),
        "logical_model": spec.logical_model,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return CompiledSkillPlan(
        contract_hash=digest, artifact_digest=artifact_digest or digest, **payload
    )


def validate_skill_composition(plans: list[CompiledSkillPlan]) -> None:
    """在发布前验证一组 Skill 的组合不变式。

    该校验放在共享契约而非 Runtime 私有实现中，使 Agent 和 Workflow
    的发布通道使用同一套依赖环、冲突、上下文、工具重叠和 Schema 规则。
    """
    if not plans:
        return
    by_id = {item.skill_id: item for item in plans}
    if len(by_id) != len(plans):
        raise ValueError("skill composition contains duplicate skill IDs")
    active_count = len(plans)
    if any(active_count > item.composition.max_active_skills for item in plans):
        raise ValueError("skill composition exceeds max_active_skills")
    total_context = sum(item.composition.estimated_context_tokens for item in plans)
    context_cap = min(item.composition.max_total_context_tokens for item in plans)
    if total_context > context_cap:
        raise ValueError("skill composition exceeds max_total_context_tokens")

    selected = set(by_id)
    edges: dict[str, list[str]] = {}
    for item in plans:
        conflicts = selected & set(item.composition.conflicts_with)
        if conflicts:
            raise ValueError(
                f"skill composition conflict: {item.skill_id}->{min(conflicts)}"
            )
        edges[item.skill_id] = [
            dependency
            for dependency in item.composition.allowed_dependencies
            if dependency in selected
        ]
    _reject_skill_cycles(edges)

    for parent_id, dependencies in edges.items():
        parent = by_id[parent_id]
        for dependency_id in dependencies:
            dependency = by_id[dependency_id]
            output_type = parent.output_schema.get("type")
            input_type = dependency.input_schema.get("type")
            if output_type and input_type and output_type != input_type:
                raise ValueError(
                    f"skill schema is incompatible: {parent_id}->{dependency_id}"
                )

    tool_owners: dict[tuple[str, str], CompiledSkillPlan] = {}
    for item in sorted(plans, key=lambda value: value.composition.priority):
        for tool in item.tools:
            key = (tool.tool_name, tool.version)
            previous = tool_owners.get(key)
            if previous is not None and not (
                previous.composition.allow_tool_overlap
                and item.composition.allow_tool_overlap
            ):
                raise ValueError(
                    "skill tool overlap is forbidden: "
                    f"{previous.skill_id}/{item.skill_id}/{tool.tool_name}:{tool.version}"
                )
            tool_owners[key] = item


def validate_skill_catalog(plans: list[CompiledSkillPlan]) -> None:
    """验证一个 Agent/Workflow 可见 Skill 目录，不把“可见”误当成“同时激活”。

    目录阶段只能判断身份重复、已选依赖环和声明的输出/输入 Schema。
    数量、冲突、上下文与工具重叠只对 Resolver 实际激活的组合有意义，
    由 ``validate_skill_composition`` 执行。
    """
    by_id = {item.skill_id: item for item in plans}
    if len(by_id) != len(plans):
        raise ValueError("skill catalog contains duplicate skill IDs")
    selected = set(by_id)
    edges = {
        item.skill_id: [
            dependency
            for dependency in item.composition.allowed_dependencies
            if dependency in selected
        ]
        for item in plans
    }
    _reject_skill_cycles(edges)
    for parent_id, dependencies in edges.items():
        parent = by_id[parent_id]
        for dependency_id in dependencies:
            dependency = by_id[dependency_id]
            output_type = parent.output_schema.get("type")
            input_type = dependency.input_schema.get("type")
            if output_type and input_type and output_type != input_type:
                raise ValueError(
                    f"skill schema is incompatible: {parent_id}->{dependency_id}"
                )


def _reject_skill_cycles(edges: dict[str, list[str]]) -> None:
    """用确定性 DFS 拒绝已选 Skill 依赖环，不跟随未激活的可选依赖。"""
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(skill_id: str) -> None:
        """将一个 Skill 标记为正在访问，再递归检查其已选依赖。"""
        if skill_id in visiting:
            raise ValueError(f"skill composition contains a cycle at: {skill_id}")
        if skill_id in visited:
            return
        visiting.add(skill_id)
        for dependency in edges.get(skill_id, []):
            visit(dependency)
        visiting.remove(skill_id)
        visited.add(skill_id)

    for skill_id in sorted(edges):
        visit(skill_id)

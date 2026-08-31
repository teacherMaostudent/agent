"""Control Plane write model and release boundary.

This service owns mutable drafts and turns approved versions into immutable
runtime snapshots.  Runtime never derives execution policy from a draft, so a
deployment cannot change behaviour halfway through an agent run.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any
from uuid import uuid4

from platform_sdk.contracts.capabilities import required_runtime_capabilities
from platform_sdk.contracts.execution_profile import resolve_execution_profile
from platform_sdk.contracts.runtime_snapshot import (
    RuntimeSnapshotCompileError,
    compile_runtime_snapshot,
)
from platform_sdk.contracts.skills import (
    CapabilityProviderKind,
    SkillCard,
    SkillGovernanceProfile,
    compile_skill_plan,
    validate_skill_catalog,
)
from platform_sdk.contracts.workflow import compile_workflow_plan

from app.application.exceptions import (
    ConflictError,
    DraftValidationError,
    ForbiddenError,
    InvalidStateError,
    NotFoundError,
    PolicyViolationError,
)
from app.domain.models import (
    AgentCreate,
    AgentDefinition,
    AgentDraftUpdate,
    AgentVersion,
    AgentVersionPublish,
    Identity,
    OutboxEvent,
    OutboxList,
    PublishedSnapshot,
    ReleaseCreate,
    ReleaseManifest,
    ReleasePromote,
    ReleaseStatus,
    RuntimeResolution,
    SkillCreate,
    SkillDefinition,
    SkillDraftUpdate,
    SkillRuntimeResolution,
    SkillStatus,
    SkillStatusUpdate,
    SkillVersion,
    SkillVersionPublish,
    Tenant,
    TenantCreate,
    TenantUpdate,
    TenantPolicy,
    TenantPolicyUpdate,
    ToolBinding,
    ValidationReport,
    WorkflowCreate,
    WorkflowDefinition,
    WorkflowDraftUpdate,
    WorkflowRelease,
    WorkflowReleaseCreate,
    WorkflowRuntimeResolution,
    WorkflowVersion,
    WorkflowVersionPublish,
    utc_now,
)
from app.domain.validation import validate_agent_spec
from app.infrastructure.sqlite_repository import SqliteRepository


class ControlPlaneService:
    """Coordinates agent definition, publication and promotion transactions.

    External quality gates and the Tool Catalog are checked before a snapshot
    is made visible.  Repository writes and outbox events stay in the same
    transaction, which makes downstream governance delivery retryable.
    """

    def __init__(
        self,
        repository: SqliteRepository,
        *,
        governance=None,
        require_quality_gate: bool = False,
        require_knowledge_contracts: bool = False,
        agent_lab=None,
        require_agent_lab: bool = False,
        tool_catalog_validator=None,
        runtime_executor_catalog=None,
        gateway_policy=None,
    ) -> None:
        """注入持久化和发布前置校验依赖；依赖失效时拒绝产生不可审计发布。"""
        self._repository = repository
        self._governance = governance
        self._require_quality_gate = require_quality_gate
        self._require_knowledge_contracts = require_knowledge_contracts
        self._agent_lab = agent_lab
        self._require_agent_lab = require_agent_lab
        self._tool_catalog_validator = tool_catalog_validator
        self._runtime_executor_catalog = runtime_executor_catalog
        self._gateway_policy = gateway_policy

    async def create_agent(
        self,
        identity: Identity,
        request: AgentCreate,
        trace_id: str,
    ) -> AgentDefinition:
        """在租户范围创建可编辑 Agent Draft 和初始
        revision，并在同一事务写入创建事件。 部副作用前返回明确错误。

        Create an initial draft and its outbox fact in the same transaction.
        """
        now = utc_now()
        agent = AgentDefinition(
            tenant_id=identity.tenant_id,
            agent_id=request.agent_id,
            revision=1,
            draft=request.spec,
            created_by=identity.user_id,
            updated_by=identity.user_id,
            created_at=now,
            updated_at=now,
        )
        event = self._event(
            "AgentCreated",
            trace_id,
            identity.tenant_id,
            "agent",
            agent.agent_id,
            {"agent_id": agent.agent_id, "revision": agent.revision},
        )
        try:
            await self._repository.create_agent(agent, event)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Agent '{request.agent_id}' already exists.") from exc
        return agent

    async def get_agent(self, identity: Identity, agent_id: str) -> AgentDefinition:
        """按租户和 Agent ID 读取 Draft 聚合；不存在时返回
        NotFound，不泄露跨租户事实。 部副作用前返回明确错误。

        Return the tenant-scoped record or raise the domain not-found error.
        """
        agent = await self._repository.get_agent(identity.tenant_id, agent_id)
        if not agent:
            raise NotFoundError(f"Agent '{agent_id}' was not found.")
        return agent

    async def list_agents(self, identity: Identity) -> list[AgentDefinition]:
        """列出当前租户可见的 Agent Draft 投影，不解析 Release 或启动
        Runtime。 失败时在产生外部副作用前返回明确错误。

        List records within the caller tenant without changing release state.
        """
        return await self._repository.list_agents(identity.tenant_id)

    async def list_agent_page(
        self, identity: Identity, *, limit: int, offset: int
    ) -> tuple[list[AgentDefinition], int]:
        """返回当前租户的受限 Agent 目录页；不读取或编译任何 Snapshot。"""
        return await self._repository.list_agent_page(
            identity.tenant_id, limit=limit, offset=offset
        )

    async def create_workflow(
        self, identity: Identity, request: WorkflowCreate, trace_id: str
    ) -> WorkflowDefinition:
        """创建独立 Workflow Draft，不借用 Agent Draft
        生命周期。
        """
        if request.spec.workflow_id != request.workflow_id:
            raise PolicyViolationError("Workflow request ID must equal spec.workflow_id.")
        now = utc_now()
        item = WorkflowDefinition(
            tenant_id=identity.tenant_id,
            workflow_id=request.workflow_id,
            revision=1,
            draft=request.spec,
            created_by=identity.user_id,
            updated_by=identity.user_id,
            created_at=now,
            updated_at=now,
        )
        event = self._event(
            "WorkflowCreated",
            trace_id,
            identity.tenant_id,
            "workflow",
            item.workflow_id,
            {"workflow_id": item.workflow_id, "revision": 1},
        )
        try:
            await self._repository.create_workflow(item, event)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Workflow '{request.workflow_id}' already exists.") from exc
        return item

    async def get_workflow(self, identity: Identity, workflow_id: str) -> WorkflowDefinition:
        """读取当前租户的可变 Workflow Draft。"""
        item = await self._repository.get_workflow(identity.tenant_id, workflow_id)
        if item is None:
            raise NotFoundError(f"Workflow '{workflow_id}' was not found.")
        return item

    async def update_workflow_draft(
        self, identity: Identity, workflow_id: str, request: WorkflowDraftUpdate, trace_id: str
    ) -> WorkflowDefinition:
        """使用 CAS 更新 Workflow Draft；冻结版本不受影响。"""
        current = await self.get_workflow(identity, workflow_id)
        if request.spec.workflow_id != workflow_id:
            raise PolicyViolationError("Workflow Draft identity cannot be changed.")
        if current.revision != request.expected_revision:
            raise ConflictError("Workflow Draft revision is stale.")
        updated = current.model_copy(
            update={
                "revision": current.revision + 1,
                "draft": request.spec,
                "updated_by": identity.user_id,
                "updated_at": utc_now(),
            }
        )
        event = self._event(
            "WorkflowDraftUpdated",
            trace_id,
            identity.tenant_id,
            "workflow",
            workflow_id,
            {"workflow_id": workflow_id, "revision": updated.revision},
        )
        if not await self._repository.update_workflow(updated, request.expected_revision, event):
            raise ConflictError("Workflow Draft was changed concurrently.")
        return updated

    async def publish_workflow_version(
        self, identity: Identity, workflow_id: str, request: WorkflowVersionPublish, trace_id: str
    ) -> WorkflowVersion:
        """冻结 Workflow Draft 为不可变零 Agent 执行计划。"""
        draft = await self.get_workflow(identity, workflow_id)
        await self._validate_workflow_providers(identity, draft.draft)
        plan, digest = compile_workflow_plan(draft.draft, request.semantic_version)
        now = utc_now()
        item = WorkflowVersion(
            tenant_id=identity.tenant_id,
            version_id=f"wv_{uuid4().hex}",
            workflow_id=workflow_id,
            semantic_version=request.semantic_version,
            source_revision=draft.revision,
            artifact_digest=digest,
            plan=plan,
            published_by=identity.user_id,
            published_at=now,
        )
        event = self._event(
            "WorkflowVersionPublished",
            trace_id,
            identity.tenant_id,
            "workflow_version",
            item.version_id,
            {"workflow_id": workflow_id, "version_id": item.version_id, "artifact_digest": digest},
        )
        try:
            await self._repository.create_workflow_version(item, event)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Workflow semantic version already exists.") from exc
        return item

    async def _validate_workflow_providers(self, identity: Identity, spec) -> None:
        """发布前验证 Workflow 的 Tool/Skill Provider
        确实存在且 Skill 已 Active。
        """
        tool_bindings = [
            {"tool_name": item.provider_id, "version": item.version}
            for item in spec.capability_providers
            if item.kind == CapabilityProviderKind.TOOL
        ]
        if self._tool_catalog_validator is not None and tool_bindings:
            try:
                self._tool_catalog_validator.validate_bindings(tool_bindings)
            except ValueError as exc:
                raise PolicyViolationError(str(exc)) from exc
        skill_plans = []
        for provider in spec.capability_providers:
            if provider.kind != CapabilityProviderKind.SKILL:
                continue
            skill = await self._repository.get_skill_version_by_semantic(
                identity.tenant_id, provider.provider_id, provider.version
            )
            if (
                skill is None
                or skill.status != SkillStatus.ACTIVE
                or not provider.artifact_digest
                or provider.artifact_digest != skill.artifact_digest
            ):
                raise PolicyViolationError(
                    "Workflow Skill provider must reference an exact Active artifact: "
                    f"{provider.provider_id}:{provider.version}."
                )
            skill_plans.append(skill.plan)
        try:
            validate_skill_catalog(skill_plans)
        except ValueError as exc:
            raise PolicyViolationError(f"Workflow Skill composition is invalid: {exc}") from exc

    async def create_workflow_release(
        self, identity: Identity, workflow_id: str, request: WorkflowReleaseCreate, trace_id: str
    ) -> WorkflowRelease:
        """激活冻结 WorkflowVersion；同环境旧版本在同事务退役。"""
        version = await self._repository.get_workflow_version(
            identity.tenant_id, workflow_id, request.version_id
        )
        if version is None:
            raise NotFoundError(f"Workflow version '{request.version_id}' was not found.")
        item = WorkflowRelease(
            tenant_id=identity.tenant_id,
            release_id=f"wrel_{uuid4().hex}",
            workflow_id=workflow_id,
            version_id=version.version_id,
            environment=request.environment,
            created_by=identity.user_id,
            created_at=utc_now(),
        )
        event = self._event(
            "WorkflowReleaseActivated",
            trace_id,
            identity.tenant_id,
            "workflow_release",
            item.release_id,
            {
                "workflow_id": workflow_id,
                "version_id": version.version_id,
                "environment": request.environment,
                "artifact_digest": version.artifact_digest,
            },
        )
        await self._repository.create_workflow_release(item, event)
        return item

    async def resolve_workflow(
        self, identity: Identity, workflow_id: str, environment: str
    ) -> WorkflowRuntimeResolution:
        """向 Runtime 返回同一事务快照中的 Active Release
        与冻结计划。
        """
        resolved = await self._repository.resolve_workflow_release(
            identity.tenant_id, workflow_id, environment
        )
        if resolved is None:
            raise NotFoundError(f"No active Workflow release for '{workflow_id}:{environment}'.")
        release, version = resolved
        return WorkflowRuntimeResolution(
            tenant_id=identity.tenant_id,
            workflow_id=workflow_id,
            environment=environment,
            release_id=release.release_id,
            version_id=version.version_id,
            plan=version.plan,
            artifact_digest=version.artifact_digest,
        )

    async def create_skill(
        self, identity: Identity, request: SkillCreate, trace_id: str
    ) -> SkillDefinition:
        """创建可编辑 Skill 草稿，并以 Outbox 事件保留其租户级创建事实。"""
        now = utc_now()
        skill = SkillDefinition(
            tenant_id=identity.tenant_id,
            skill_id=request.skill_id,
            revision=1,
            draft=request.spec,
            created_by=identity.user_id,
            updated_by=identity.user_id,
            created_at=now,
            updated_at=now,
        )
        event = self._event(
            "SkillCreated",
            trace_id,
            identity.tenant_id,
            "skill",
            skill.skill_id,
            {"skill_id": skill.skill_id, "revision": skill.revision},
        )
        try:
            await self._repository.create_skill(skill, event)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Skill '{request.skill_id}' already exists.") from exc
        return skill

    async def get_skill(self, identity: Identity, skill_id: str) -> SkillDefinition:
        """读取当前租户的 Skill 草稿；发布执行从不使用此可变对象。"""
        skill = await self._repository.get_skill(identity.tenant_id, skill_id)
        if not skill:
            raise NotFoundError(f"Skill '{skill_id}' was not found.")
        return skill

    async def update_skill_draft(
        self, identity: Identity, skill_id: str, request: SkillDraftUpdate, trace_id: str
    ) -> SkillDefinition:
        """基于修订号 CAS 更新 Skill 草稿，避免并发编辑丢失。"""
        current = await self.get_skill(identity, skill_id)
        if current.revision != request.expected_revision:
            raise ConflictError(
                "Skill draft revision is stale.",
                expected_revision=request.expected_revision,
                current_revision=current.revision,
            )
        if request.spec.skill_id != skill_id:
            raise DraftValidationError(
                ValidationReport(
                    valid=False,
                    issues=[
                        {
                            "severity": "error",
                            "code": "skill.identity_mismatch",
                            "path": "skill_id",
                            "message": "Skill draft ID must equal the target Skill ID.",
                        }
                    ],
                )
            )
        updated = current.model_copy(
            update={
                "revision": current.revision + 1,
                "draft": request.spec,
                "updated_by": identity.user_id,
                "updated_at": utc_now(),
            }
        )
        event = self._event(
            "SkillDraftUpdated",
            trace_id,
            identity.tenant_id,
            "skill",
            skill_id,
            {
                "skill_id": skill_id,
                "previous_revision": current.revision,
                "revision": updated.revision,
            },
        )
        if not await self._repository.update_skill(updated, request.expected_revision, event):
            raise ConflictError("Skill draft was changed concurrently.")
        return updated

    async def publish_skill_version(
        self, identity: Identity, skill_id: str, request: SkillVersionPublish, trace_id: str
    ) -> SkillVersion:
        """编译 Skill 草稿为不可变计划；没有运行期动态下载或重新解释。"""
        skill = await self.get_skill(identity, skill_id)
        if self._tool_catalog_validator is not None and skill.draft.tools:
            try:
                catalog_items = self._tool_catalog_validator.resolve_catalog_items(
                    [item.model_dump(mode="json") for item in skill.draft.tools]
                )
                self._validate_skill_tool_risk(
                    skill.draft.resolved_governance_profile(), catalog_items
                )
            except ValueError as exc:
                raise PolicyViolationError(str(exc)) from exc
        plan = compile_skill_plan(skill.draft, version=request.semantic_version)
        now = utc_now()
        version = SkillVersion(
            tenant_id=identity.tenant_id,
            version_id=f"sv_{uuid4().hex}",
            skill_id=skill_id,
            semantic_version=request.semantic_version,
            source_revision=skill.revision,
            artifact_digest=plan.artifact_digest,
            plan=plan,
            change_summary=request.change_summary,
            published_by=identity.user_id,
            published_at=now,
            updated_at=now,
        )
        event = self._event(
            "SkillVersionPublished",
            trace_id,
            identity.tenant_id,
            "skill_version",
            version.version_id,
            {
                "skill_id": skill_id,
                "version_id": version.version_id,
                "semantic_version": version.semantic_version,
                "artifact_digest": version.artifact_digest,
            },
        )
        try:
            await self._repository.create_skill_version(version, event)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                f"Skill semantic version already exists: {skill_id}:{request.semantic_version}."
            ) from exc
        return version

    @staticmethod
    def _validate_skill_tool_risk(
        profile: SkillGovernanceProfile, catalog_items: list[dict[str, Any]]
    ) -> None:
        """用 Tool Catalog 的真实风险校验 Skill Profile，防止
        Skill 自报低风险。
        """
        if not catalog_items:
            return
        write_items = [item for item in catalog_items if str(item.get("risk")) != "read_only"]
        high_items = [
            item
            for item in catalog_items
            if str(item.get("risk")) in {"write_high_risk", "human_approval_required"}
        ]
        if profile == SkillGovernanceProfile.READ_ONLY and write_items:
            raise ValueError("READ_ONLY skill cannot bind a write tool")
        if profile == SkillGovernanceProfile.REVERSIBLE_WRITE and high_items:
            raise ValueError("REVERSIBLE_WRITE skill cannot hide a high-risk tool")
        if high_items and profile not in {
            SkillGovernanceProfile.HIGH_RISK_WRITE,
            SkillGovernanceProfile.HUMAN_APPROVAL_REQUIRED,
        }:
            raise ValueError("high-risk tool requires a high-risk Skill governance profile")
        if profile == SkillGovernanceProfile.HUMAN_APPROVAL_REQUIRED and any(
            not bool(item.get("approval_required")) for item in write_items
        ):
            raise ValueError("human-approval Skill contains a write tool without approval")

    async def update_skill_status(
        self,
        identity: Identity,
        skill_id: str,
        version_id: str,
        request: SkillStatusUpdate,
        trace_id: str,
    ) -> SkillVersion:
        """执行受限 Skill 生命周期迁移；隔离/退役优先于后续 Agent 调用。"""
        version = await self._repository.get_skill_version(identity.tenant_id, skill_id, version_id)
        if version is None:
            raise NotFoundError(
                f"Skill version '{version_id}' was not found for Skill '{skill_id}'."
            )
        allowed = {
            SkillStatus.VALIDATING: {
                SkillStatus.CANDIDATE,
                SkillStatus.QUARANTINED,
                SkillStatus.RETIRED,
            },
            SkillStatus.CANDIDATE: {
                SkillStatus.CANARY,
                SkillStatus.RETIRED,
                SkillStatus.QUARANTINED,
            },
            SkillStatus.CANARY: {
                SkillStatus.ACTIVE,
                SkillStatus.CANDIDATE,
                SkillStatus.QUARANTINED,
                SkillStatus.RETIRED,
            },
            SkillStatus.ACTIVE: {
                SkillStatus.DEPRECATED,
                SkillStatus.DEGRADED,
                SkillStatus.QUARANTINED,
                SkillStatus.RETIRED,
            },
            SkillStatus.DEGRADED: {
                SkillStatus.ACTIVE,
                SkillStatus.QUARANTINED,
                SkillStatus.RETIRED,
            },
            SkillStatus.DEPRECATED: {SkillStatus.RETIRED, SkillStatus.QUARANTINED},
            SkillStatus.QUARANTINED: {SkillStatus.RETIRED},
            SkillStatus.RETIRED: set(),
        }
        if request.status not in allowed[version.status]:
            raise InvalidStateError(
                "Skill status transition is not allowed: "
                f"{version.status.value}->{request.status.value}."
            )
        if request.status in {
            SkillStatus.CANDIDATE,
            SkillStatus.CANARY,
            SkillStatus.ACTIVE,
        }:
            if not request.quality_gate_run_id:
                raise PolicyViolationError(
                    "A passing Governance quality-gate run is required for Skill qualification."
                )
            if self._governance is None:
                raise InvalidStateError("Governance quality-gate client is unavailable.")
            gate = await self._governance.quality_gate(
                identity.tenant_id, request.quality_gate_run_id
            )
            if not gate.get("passed"):
                raise PolicyViolationError("Governance quality gate rejected Skill activation.")
        updated = version.model_copy(update={"status": request.status, "updated_at": utc_now()})
        event = self._event(
            "SkillVersionStatusChanged",
            trace_id,
            identity.tenant_id,
            "skill_version",
            version_id,
            {
                "skill_id": skill_id,
                "version_id": version_id,
                "previous_status": version.status.value,
                "status": request.status.value,
                "quality_gate_id": request.quality_gate_run_id,
            },
        )
        await self._repository.update_skill_status(updated, event)
        return updated

    async def resolve_skill(
        self, identity: Identity, skill_id: str, version: str
    ) -> SkillRuntimeResolution:
        """只向工作负载返回 Active 且摘要固定的 SkillVersion。"""
        item = await self._repository.get_skill_version_by_semantic(
            identity.tenant_id, skill_id, version
        )
        if item is None or item.status != SkillStatus.ACTIVE:
            raise NotFoundError(f"Active Skill '{skill_id}:{version}' was not found.")
        return SkillRuntimeResolution(
            tenant_id=identity.tenant_id,
            skill_id=skill_id,
            version=version,
            artifact_digest=item.artifact_digest,
            plan=item.plan,
        )

    async def list_skill_cards(
        self, identity: Identity, capability_id: str = ""
    ) -> list[SkillCard]:
        """只披露 Active Skill
        的摘要和能力，Prompt/工具/知识绑定在选中前不可见。
        """
        capability = capability_id.strip().upper()
        cards: list[SkillCard] = []
        for item in await self._repository.list_active_skill_versions(identity.tenant_id):
            provides = [value.capability_id for value in item.plan.provides]
            if capability and capability not in provides:
                continue
            cards.append(
                SkillCard(
                    skill_id=item.skill_id,
                    version=item.semantic_version,
                    description=item.plan.description,
                    provides=provides,
                    risk=item.plan.risk,
                )
            )
        return cards

    async def update_draft(
        self,
        identity: Identity,
        agent_id: str,
        request: AgentDraftUpdate,
        trace_id: str,
    ) -> AgentDefinition:
        """使用 expected_revision 执行 CAS 更新并写
        Outbox；并发修改时返回最新 revision 供调用方重试。
        产生外部副作用前返回明确错误。

        Reject stale revisions so concurrent editors cannot overwrite each other.
        """
        current = await self.get_agent(identity, agent_id)
        if current.revision != request.expected_revision:
            raise ConflictError(
                "Draft revision is stale.",
                expected_revision=request.expected_revision,
                current_revision=current.revision,
            )
        updated = current.model_copy(
            update={
                "revision": current.revision + 1,
                "draft": request.spec,
                "updated_by": identity.user_id,
                "updated_at": utc_now(),
            }
        )
        event = self._event(
            "AgentDraftUpdated",
            trace_id,
            identity.tenant_id,
            "agent",
            agent_id,
            {
                "agent_id": agent_id,
                "previous_revision": current.revision,
                "revision": updated.revision,
            },
        )
        changed = await self._repository.update_agent(updated, request.expected_revision, event)
        if not changed:
            latest = await self.get_agent(identity, agent_id)
            raise ConflictError(
                "Draft was changed concurrently.",
                expected_revision=request.expected_revision,
                current_revision=latest.revision,
            )
        return updated

    async def validate_draft(self, identity: Identity, agent_id: str) -> ValidationReport:
        """按租户策略校验 Draft 的模型、知识、工具、Skill、Workflow
        和风险约束；只返回报告，不修改 Draft。 产生外部副作用前返回明确错误。

        Validate release inputs against tenant policy without mutating the draft.
        """
        agent = await self.get_agent(identity, agent_id)
        policy = await self.get_tenant_policy(identity)
        return validate_agent_spec(agent.draft, policy)

    async def publish_version(
        self,
        identity: Identity,
        agent_id: str,
        request: AgentVersionPublish,
        trace_id: str,
    ) -> AgentVersion:
        """验证 Draft 与绑定目录版本后冻结不可变
        AgentVersion；任何发布依赖缺失都在写版本前失败。 用前返回明确错误。

        Freeze a validated draft only after its bound tool versions are verified.
        """
        agent = await self.get_agent(identity, agent_id)
        policy = await self.get_tenant_policy(identity)
        report = validate_agent_spec(agent.draft, policy)
        if not report.valid:
            raise DraftValidationError(report)
        if self._tool_catalog_validator is not None:
            try:
                self._tool_catalog_validator.validate_bindings(
                    [item.model_dump(mode="json") for item in agent.draft.tools]
                )
            except ValueError as exc:
                raise DraftValidationError(
                    ValidationReport(
                        valid=False,
                        issues=[
                            {
                                "severity": "error",
                                "code": "tools.catalog_missing",
                                "path": "tools",
                                "message": str(exc),
                            }
                        ],
                    )
                ) from exc
        frozen_spec = agent.draft
        if self._tool_catalog_validator is not None:
            try:
                resolved_tools = self._tool_catalog_validator.resolve_bindings(
                    [item.model_dump(mode="json") for item in agent.draft.tools]
                )
                frozen_spec = agent.draft.model_copy(
                    update={"tools": [ToolBinding.model_validate(item) for item in resolved_tools]}
                )
            except ValueError as exc:
                raise DraftValidationError(
                    ValidationReport(
                        valid=False,
                        issues=[
                            {
                                "severity": "error",
                                "code": "tools.catalog_contract_invalid",
                                "path": "tools",
                                "message": str(exc),
                            }
                        ],
                    )
                ) from exc
            frozen_report = validate_agent_spec(frozen_spec, policy)
            if not frozen_report.valid:
                raise DraftValidationError(frozen_report)
        await self._validate_skill_bindings(identity, agent.draft)
        await self._validate_agent_workflow_providers(identity, agent.draft)

        now = utc_now()
        version_id = f"av_{uuid4().hex}"
        component_hashes = _component_hashes(frozen_spec.model_dump(mode="json"))
        snapshot = PublishedSnapshot(
            tenant_id=identity.tenant_id,
            agent_id=agent_id,
            agent_version=f"{agent_id}:{request.semantic_version}",
            graph_version=f"{agent.draft.graph.graph_id}:{agent.revision}",
            prompt_version=f"{agent.draft.prompt.prompt_id}:{agent.revision}",
            knowledge_version=f"kb:{component_hashes['knowledge'][:12]}",
            tool_set_version=f"tools:{component_hashes['tools'][:12]}",
            model_policy_version=f"{agent.draft.model_policy.policy_id}:{agent.revision}",
            spec=frozen_spec,
            published_at=now,
        )
        # 发布事务冻结 Runtime 可执行产物; 请求运行时不得再解释可变草稿或重新编译。
        try:
            artifact = compile_runtime_snapshot(
                snapshot.model_dump(mode="json"),
                tenant_id=identity.tenant_id,
                agent_id=agent_id,
            )
        except RuntimeSnapshotCompileError as exc:
            raise DraftValidationError(
                ValidationReport(
                    valid=False,
                    issues=[
                        {
                            "severity": "error",
                            "code": "runtime_snapshot.compile_failed",
                            "path": "runtime_executor",
                            "message": str(exc),
                        }
                    ],
                )
            ) from exc
        snapshot = snapshot.model_copy(
            update={"runtime_artifact": artifact.model_dump(mode="json")}
        )
        content_hash = _hash(snapshot.model_dump(mode="json"))
        version = AgentVersion(
            tenant_id=identity.tenant_id,
            version_id=version_id,
            agent_id=agent_id,
            semantic_version=request.semantic_version,
            source_revision=agent.revision,
            content_hash=content_hash,
            snapshot=snapshot,
            change_summary=request.change_summary,
            published_by=identity.user_id,
            published_at=now,
        )
        event = self._event(
            "AgentVersionPublished",
            trace_id,
            identity.tenant_id,
            "agent_version",
            version_id,
            {
                "agent_id": agent_id,
                "version_id": version_id,
                "semantic_version": request.semantic_version,
                "content_hash": content_hash,
                "source_revision": agent.revision,
            },
        )
        try:
            await self._repository.create_version(version, event)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                f"Semantic version '{request.semantic_version}' already exists "
                f"for agent '{agent_id}'."
            ) from exc
        return version

    async def _validate_agent_workflow_providers(self, identity: Identity, spec) -> None:
        """验证 Agent 可调用 Workflow 的版本和摘要，防止动态 latest
        漂移。
        """
        for provider in spec.capability_providers:
            if provider.kind != CapabilityProviderKind.WORKFLOW:
                continue
            workflow = await self._repository.get_workflow_version_by_semantic(
                identity.tenant_id, provider.provider_id, provider.version
            )
            if (
                workflow is None
                or not provider.artifact_digest
                or workflow.artifact_digest != provider.artifact_digest
            ):
                raise PolicyViolationError(
                    "Agent Workflow provider must reference an exact published artifact: "
                    f"{provider.provider_id}:{provider.version}."
                )

    async def _validate_skill_bindings(self, identity: Identity, draft) -> None:
        """确认 Agent 绑定的每个 SkillVersion
        存在、已准入且摘要完全匹配。
        """
        skill_plans = []
        for binding in draft.skills:
            version = await self._repository.get_skill_version_by_semantic(
                identity.tenant_id, binding.skill_id, binding.version
            )
            if version is None:
                raise DraftValidationError(
                    ValidationReport(
                        valid=False,
                        issues=[
                            {
                                "severity": "error",
                                "code": "skills.catalog_missing",
                                "path": "skills",
                                "message": "Skill does not exist: "
                                f"{binding.skill_id}:{binding.version}.",
                            }
                        ],
                    )
                )
            if version.status.value != "active":
                raise DraftValidationError(
                    ValidationReport(
                        valid=False,
                        issues=[
                            {
                                "severity": "error",
                                "code": "skills.not_active",
                                "path": "skills",
                                "message": "Skill is not active: "
                                f"{binding.skill_id}:{binding.version}.",
                            }
                        ],
                    )
                )
            if version.artifact_digest != binding.artifact_digest:
                raise DraftValidationError(
                    ValidationReport(
                        valid=False,
                        issues=[
                            {
                                "severity": "error",
                                "code": "skills.digest_mismatch",
                                "path": "skills",
                                "message": "Skill artifact digest does not match: "
                                f"{binding.skill_id}:{binding.version}.",
                            }
                        ],
                    )
                )
            skill_plans.append(version.plan)
        try:
            validate_skill_catalog(skill_plans)
        except ValueError as exc:
            raise DraftValidationError(
                ValidationReport(
                    valid=False,
                    issues=[
                        {
                            "severity": "error",
                            "code": "skills.composition_invalid",
                            "path": "skills",
                            "message": str(exc),
                        }
                    ],
                )
            ) from exc

    async def list_versions(self, identity: Identity, agent_id: str) -> list[AgentVersion]:
        """确认 Agent 属于调用方租户后列出其冻结版本，不触发任何状态变化。"""
        await self.get_agent(identity, agent_id)
        return await self._repository.list_versions(identity.tenant_id, agent_id)

    async def get_version(
        self,
        identity: Identity,
        agent_id: str,
        version_id: str,
    ) -> AgentVersion:
        """按租户与 Agent 双重约束读取版本，避免仅凭全局 ID 跨域访问。"""
        version = await self._repository.get_version(identity.tenant_id, agent_id, version_id)
        if not version:
            raise NotFoundError(f"Version '{version_id}' was not found for agent '{agent_id}'.")
        return version

    async def create_release(
        self,
        identity: Identity,
        agent_id: str,
        request: ReleaseCreate,
        trace_id: str,
    ) -> ReleaseManifest:
        """创建发布记录并原子写入 Outbox，且在可见前通过质量门禁与灰度约束。"""
        version = await self.get_version(identity, agent_id, request.version_id)
        gate: dict[str, Any] = {}
        agent_lab_evidence: dict[str, Any] = {}
        runtime_executor: dict[str, Any] = {}
        if self._runtime_executor_catalog is not None:
            try:
                _, resolved_executor_profile = resolve_execution_profile(
                    version.snapshot.spec.model_dump(mode="json")
                )
                runtime_executor = self._runtime_executor_catalog.validate(
                    request.environment,
                    resolved_executor_profile,
                    required_capabilities=required_runtime_capabilities(
                        version.snapshot.spec.model_dump(mode="json")
                    ),
                )
            except ValueError as exc:
                raise PolicyViolationError(
                    f"Runtime executor availability rejected release: {exc}"
                ) from exc
        if self._require_agent_lab and not request.agent_lab_experiment_id:
            raise PolicyViolationError(
                "A passing Agent Lab experiment is required for Agent release."
            )
        if request.agent_lab_experiment_id:
            if self._agent_lab is None:
                raise InvalidStateError("Agent Lab client is unavailable.")
            try:
                agent_lab_evidence = await self._agent_lab.approved_release_evidence(
                    identity.tenant_id, request.agent_lab_experiment_id
                )
            except Exception as exc:
                raise PolicyViolationError(f"Agent Lab evidence rejected release: {exc}") from exc
            if agent_lab_evidence.get("agentId") != agent_id:
                raise PolicyViolationError("Agent Lab experiment belongs to another Agent.")
            if agent_lab_evidence.get("versionId") != version.version_id:
                raise PolicyViolationError("Agent Lab experiment was not run against this version.")
            if agent_lab_evidence.get("environment") != "laboratory":
                raise PolicyViolationError(
                    "Agent Lab evidence must originate from laboratory environment."
                )
            if request.quality_gate_run_id != agent_lab_evidence.get("judgeRunId"):
                raise PolicyViolationError(
                    "Release quality-gate run must be the Judge run owned by Agent Lab."
                )
        if self._require_quality_gate and not request.quality_gate_run_id:
            raise PolicyViolationError(
                "A Governance quality-gate run is required for Agent release."
            )
        if request.quality_gate_run_id:
            if self._governance is None:
                raise InvalidStateError("Governance quality-gate client is unavailable.")
            gate = await self._governance.quality_gate(
                identity.tenant_id, request.quality_gate_run_id
            )
            if not gate.get("passed"):
                raise PolicyViolationError(
                    "Governance quality gate rejected the Agent release.",
                    quality_gate_id=gate.get("id"),
                    reasons=gate.get("reasons") or [],
                )
        if self._require_knowledge_contracts:
            self._validate_knowledge_release_contract(version.snapshot, gate)
        releases = await self._repository.list_releases(
            identity.tenant_id,
            agent_id,
            request.environment,
        )
        active = [item for item in releases if item.status == ReleaseStatus.ACTIVE]
        if any(item.rollout_percentage < 100 for item in active):
            raise ConflictError("An active canary release already exists for this environment.")

        baseline = active[0] if active else None
        if baseline and baseline.version_id == version.version_id:
            raise ConflictError("This version is already active in the target environment.")
        policy = await self.get_tenant_policy(identity)
        is_canary = baseline is not None and request.rollout_percentage < 100
        if is_canary and request.rollout_percentage > policy.max_canary_percentage:
            raise PolicyViolationError(
                "Canary percentage exceeds the tenant policy.",
                requested=request.rollout_percentage,
                allowed=policy.max_canary_percentage,
            )

        rollout_percentage = request.rollout_percentage if baseline else 100
        now = utc_now()
        release = ReleaseManifest(
            tenant_id=identity.tenant_id,
            release_id=f"rel_{uuid4().hex}",
            agent_id=agent_id,
            version_id=version.version_id,
            environment=request.environment,
            rollout_percentage=rollout_percentage,
            tenant_allowlist=request.tenant_allowlist,
            status=ReleaseStatus.ACTIVE,
            previous_release_id=baseline.release_id if baseline else None,
            reason=request.reason,
            quality_gate_id=gate.get("id"),
            quality_gate_metrics=gate.get("metrics") or {},
            agent_lab_experiment_id=request.agent_lab_experiment_id,
            runtime_executor_catalog_version=runtime_executor.get("catalog_version"),
            runtime_executor_cluster_id=runtime_executor.get("cluster_id"),
            runtime_executor_catalog_hash=runtime_executor.get("catalog_hash"),
            runtime_capability_manifest_digest=runtime_executor.get("capability_manifest_digest"),
            created_by=identity.user_id,
            created_at=now,
            updated_at=now,
        )
        event_type = (
            "ReleaseActivated"
            if not baseline or rollout_percentage == 100
            else "ReleaseCanaryStarted"
        )
        event = self._event(
            event_type,
            trace_id,
            identity.tenant_id,
            "release",
            release.release_id,
            {
                "agent_id": agent_id,
                "release_id": release.release_id,
                "version_id": version.version_id,
                "environment": release.environment,
                "rollout_percentage": release.rollout_percentage,
                "previous_release_id": release.previous_release_id,
                "quality_gate_id": release.quality_gate_id,
                "agent_lab_experiment_id": release.agent_lab_experiment_id,
                "runtime_executor_catalog_version": release.runtime_executor_catalog_version,
                "runtime_executor_cluster_id": release.runtime_executor_cluster_id,
                "runtime_executor_catalog_hash": release.runtime_executor_catalog_hash,
                "runtime_capability_manifest_digest": release.runtime_capability_manifest_digest,
            },
        )
        await self._repository.create_release(
            release,
            event,
            retire_release_id=baseline.release_id
            if baseline and rollout_percentage == 100
            else None,
        )
        return release

    @staticmethod
    def _validate_knowledge_release_contract(
        snapshot: PublishedSnapshot, gate: dict[str, Any]
    ) -> None:
        """要求发布知识绑定固定索引空间，并由已通过的检索质量门禁提供证据。"""
        knowledge = snapshot.spec.knowledge
        if not knowledge:
            return
        missing = [
            binding.knowledge_base
            for binding in knowledge
            if not binding.index_version
            or not binding.embedding_contract_id
            or not binding.retrieval_evaluation_id
        ]
        if missing:
            raise PolicyViolationError(
                "Knowledge bindings require index_version, embedding_contract_id and "
                "retrieval_evaluation_id.",
                knowledge_bases=missing,
            )
        retrieval = gate.get("metrics", {}).get("retrieval", {}) if gate else {}
        if not retrieval or float(retrieval.get("recallAtK", 0)) < 1:
            raise PolicyViolationError(
                "Knowledge release requires a passing retrieval Recall@K quality gate."
            )

    async def list_releases(
        self,
        identity: Identity,
        agent_id: str,
        environment: str | None = None,
    ) -> list[ReleaseManifest]:
        """在租户范围内查询 Agent 的发布历史，环境过滤不影响授权边界。"""
        await self.get_agent(identity, agent_id)
        return await self._repository.list_releases(identity.tenant_id, agent_id, environment)

    async def get_release(self, identity: Identity, release_id: str) -> ReleaseManifest:
        """读取租户发布记录；缺失时显式拒绝而不泄漏其他租户的存在性。"""
        release = await self._repository.get_release(identity.tenant_id, release_id)
        if not release:
            raise NotFoundError(f"Release '{release_id}' was not found.")
        return release

    async def promote_release(
        self,
        identity: Identity,
        release_id: str,
        request: ReleasePromote,
        trace_id: str,
    ) -> ReleaseManifest:
        """以乐观并发控制提升灰度比例，禁止将提升接口用于降级流量。"""
        release = await self.get_release(identity, release_id)
        if release.status != ReleaseStatus.ACTIVE:
            raise InvalidStateError(
                "Only an active release can be promoted.", status=release.status.value
            )
        if request.rollout_percentage < release.rollout_percentage:
            raise InvalidStateError(
                "Promotion cannot reduce rollout percentage; "
                "pause or rollback the release instead.",
                current=release.rollout_percentage,
                requested=request.rollout_percentage,
            )
        policy = await self.get_tenant_policy(identity)
        if (
            request.rollout_percentage < 100
            and request.rollout_percentage > policy.max_canary_percentage
        ):
            raise PolicyViolationError(
                "Canary percentage exceeds the tenant policy.",
                requested=request.rollout_percentage,
                allowed=policy.max_canary_percentage,
            )

        updated = release.model_copy(
            update={
                "rollout_percentage": request.rollout_percentage,
                "updated_at": utc_now(),
            }
        )
        event_type = (
            "ReleasePromoted" if request.rollout_percentage == 100 else "ReleaseRolloutUpdated"
        )
        event = self._event(
            event_type,
            trace_id,
            identity.tenant_id,
            "release",
            release_id,
            {
                "agent_id": release.agent_id,
                "release_id": release_id,
                "previous_percentage": release.rollout_percentage,
                "rollout_percentage": request.rollout_percentage,
            },
        )
        updated_ok = await self._repository.update_release(
            updated,
            event,
            related_release_id=release.previous_release_id
            if request.rollout_percentage == 100
            else None,
            related_status=ReleaseStatus.RETIRED if request.rollout_percentage == 100 else None,
            expected_updated_at=release.updated_at.isoformat(),
        )
        if not updated_ok:
            raise ConflictError("Release changed concurrently; reload and retry.")
        return updated

    async def pause_release(
        self,
        identity: Identity,
        release_id: str,
        trace_id: str,
    ) -> ReleaseManifest:
        """暂停具有回退基线的活动发布；首个稳定版本不能被暂停以免无可用版本。"""
        release = await self.get_release(identity, release_id)
        if release.status != ReleaseStatus.ACTIVE:
            raise InvalidStateError(
                "Only an active release can be paused.", status=release.status.value
            )
        if not release.previous_release_id:
            raise InvalidStateError("The first stable release cannot be paused without a fallback.")
        updated = release.model_copy(
            update={"status": ReleaseStatus.PAUSED, "updated_at": utc_now()}
        )
        event = self._event(
            "ReleasePaused",
            trace_id,
            identity.tenant_id,
            "release",
            release_id,
            {"agent_id": release.agent_id, "release_id": release_id},
        )
        updated_ok = await self._repository.update_release(
            updated,
            event,
            expected_updated_at=release.updated_at.isoformat(),
        )
        if not updated_ok:
            raise ConflictError("Release changed concurrently; reload and retry.")
        return updated

    async def rollback_release(
        self,
        identity: Identity,
        release_id: str,
        trace_id: str,
    ) -> ReleaseManifest:
        """原子标记当前发布回滚并恢复基线发布；并发修改会要求调用方重试。"""
        release = await self.get_release(identity, release_id)
        if release.status not in {ReleaseStatus.ACTIVE, ReleaseStatus.PAUSED}:
            raise InvalidStateError(
                "Only an active or paused release can be rolled back.",
                status=release.status.value,
            )
        if not release.previous_release_id:
            raise InvalidStateError("The first release has no rollback target.")
        previous = await self.get_release(identity, release.previous_release_id)
        updated = release.model_copy(
            update={"status": ReleaseStatus.ROLLED_BACK, "updated_at": utc_now()}
        )
        event = self._event(
            "ReleaseRolledBack",
            trace_id,
            identity.tenant_id,
            "release",
            release_id,
            {
                "agent_id": release.agent_id,
                "release_id": release_id,
                "restored_release_id": previous.release_id,
            },
        )
        updated_ok = await self._repository.update_release(
            updated,
            event,
            related_release_id=previous.release_id,
            related_status=ReleaseStatus.ACTIVE,
            expected_updated_at=release.updated_at.isoformat(),
        )
        if not updated_ok:
            raise ConflictError("Release changed concurrently; reload and retry.")
        return updated

    async def resolve_runtime(
        self,
        identity: Identity,
        agent_id: str,
        environment: str,
        session_id: str,
    ) -> RuntimeResolution:
        """按租户、环境和 Session 稳定解析已发布
        Snapshot；已有会话绑定不会随新灰度发布漂移。
        生外部副作用前返回明确错误。

        Resolve a stable execution snapshot for one tenant and session binding.
        """
        await self.get_agent(identity, agent_id)
        binding = await self._repository.get_session_binding(
            identity.tenant_id,
            agent_id,
            environment,
            session_id,
        )
        if binding:
            bound_release = await self._repository.get_release(
                identity.tenant_id,
                binding["release_id"],
            )
            if bound_release and bound_release.status != ReleaseStatus.ROLLED_BACK:
                return await self._resolution(
                    identity,
                    bound_release,
                    environment,
                    session_id,
                    "pinned",
                    True,
                )

        releases = await self._repository.list_releases(
            identity.tenant_id,
            agent_id,
            environment,
        )
        active = [item for item in releases if item.status == ReleaseStatus.ACTIVE]
        if not active:
            raise NotFoundError(
                f"No active release exists for agent '{agent_id}' in '{environment}'."
            )

        candidate = active[0]
        assignment = "stable"
        selected = candidate
        if candidate.previous_release_id and candidate.rollout_percentage < 100:
            previous = await self._repository.get_release(
                identity.tenant_id,
                candidate.previous_release_id,
            )
            if not previous:
                raise InvalidStateError("Canary release has no resolvable baseline.")
            if identity.tenant_id in candidate.tenant_allowlist:
                assignment = "allowlist"
            elif (
                _bucket(identity.tenant_id, agent_id, environment, session_id)
                < candidate.rollout_percentage
            ):
                assignment = "canary"
            else:
                selected = previous
                assignment = "stable"
        elif not candidate.previous_release_id:
            assignment = "first_release"

        now = utc_now().isoformat()
        await self._repository.bind_session(
            identity.tenant_id,
            agent_id,
            environment,
            session_id,
            selected.release_id,
            assignment,
            now,
        )
        return await self._resolution(
            identity,
            selected,
            environment,
            session_id,
            assignment,
            False,
        )

    async def get_release_snapshot(
        self,
        identity: Identity,
        release_id: str,
    ) -> PublishedSnapshot:
        """由租户受限的发布与版本关系返回冻结快照，运行时绝不读取草稿。"""
        release = await self.get_release(identity, release_id)
        version = await self.get_version(identity, release.agent_id, release.version_id)
        return version.snapshot

    async def get_tenant_policy(self, identity: Identity) -> TenantPolicy:
        """读取租户策略；尚未配置时返回显式默认值而非共享全局策略。"""
        policy = await self._repository.get_tenant_policy(identity.tenant_id)
        return policy or TenantPolicy(tenant_id=identity.tenant_id)

    async def list_tenants(self, identity: Identity) -> list[Tenant]:
        """列出独立租户目录；只有平台最高管理员可跨租户枚举。"""
        self._require_platform_super_admin(identity)
        return await self._repository.list_tenants()

    async def get_tenant(self, identity: Identity, tenant_id: str) -> Tenant:
        """读取目录中的一个租户；普通管理员只能读取自己的租户。"""
        if tenant_id != identity.tenant_id:
            self._require_platform_super_admin(identity)
        tenant = await self._repository.get_tenant(tenant_id)
        if tenant is None:
            raise NotFoundError(f"Tenant '{tenant_id}' was not found.")
        return tenant

    async def create_tenant(
        self, identity: Identity, request: TenantCreate, trace_id: str
    ) -> Tenant:
        """建立租户目录和默认发布策略；tenant_id 一经创建不可改名。"""
        self._require_platform_super_admin(identity)
        now = utc_now()
        tenant = Tenant(
            tenant_id=request.tenant_id,
            display_name=request.display_name,
            data_region=request.data_region,
            created_by=identity.user_id,
            created_at=now,
            updated_by=identity.user_id,
            updated_at=now,
        )
        policy = TenantPolicy(
            tenant_id=tenant.tenant_id,
            allowed_data_regions=[tenant.data_region],
            updated_by=identity.user_id,
            updated_at=now,
        )
        event = self._event(
            "TenantCreated", trace_id, tenant.tenant_id, "tenant", tenant.tenant_id,
            {"display_name": tenant.display_name, "data_region": tenant.data_region, "status": tenant.status.value},
        )
        try:
            await self._repository.create_tenant(tenant, policy, event)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Tenant '{tenant.tenant_id}' already exists.") from exc
        return tenant

    async def update_tenant(
        self, identity: Identity, tenant_id: str, request: TenantUpdate, trace_id: str
    ) -> Tenant:
        """软冻结或退休租户，并保留其历史数据与审计链。"""
        self._require_platform_super_admin(identity)
        previous = await self._repository.get_tenant(tenant_id)
        if previous is None:
            raise NotFoundError(f"Tenant '{tenant_id}' was not found.")
        now = utc_now()
        tenant = previous.model_copy(
            update={
                "display_name": request.display_name,
                "data_region": request.data_region,
                "status": request.status,
                "updated_by": identity.user_id,
                "updated_at": now,
            }
        )
        event = self._event(
            "TenantStatusChanged" if previous.status != tenant.status else "TenantUpdated",
            trace_id, tenant.tenant_id, "tenant", tenant.tenant_id,
            {
                "previous_status": previous.status.value,
                "status": tenant.status.value,
                "display_name": tenant.display_name,
                "data_region": tenant.data_region,
                "reason": request.reason,
            },
        )
        await self._repository.update_tenant(tenant, event)
        return tenant

    async def update_tenant_policy(
        self,
        identity: Identity,
        request: TenantPolicyUpdate,
        trace_id: str,
    ) -> TenantPolicy:
        """使用管理员身份更新租户发布、预算和风险策略，并写入可审计的策略变更事件。
        前返回明确错误。

        Apply a concurrency-safe update and publish its auditable state transition.
        """
        policy = TenantPolicy(
            tenant_id=identity.tenant_id,
            **request.model_dump(),
            updated_by=identity.user_id,
            updated_at=utc_now(),
        )
        event = self._event(
            "TenantPolicyUpdated",
            trace_id,
            identity.tenant_id,
            "tenant_policy",
            identity.tenant_id,
            {
                "allowed_models": policy.allowed_models,
                "allowed_data_regions": policy.allowed_data_regions,
                "max_canary_percentage": policy.max_canary_percentage,
                "llm_quota_subjects": sorted(policy.llm_quotas),
            },
        )
        previous_gateway_quotas: dict[str, Any] | None = None
        if self._gateway_policy is not None:
            # Apply a complete tenant snapshot, not a partial browser patch. If the authoritative
            # DB transaction fails, compensate Gateway with the exact prior snapshot.
            previous_gateway_quotas = await self._gateway_policy.quotas(identity.tenant_id)
            gateway_quotas = {
                subject: {
                    "dailyTokenLimit": quota.daily_token_limit,
                    "dailyCostLimit": quota.daily_cost_limit_usd,
                }
                for subject, quota in policy.llm_quotas.items()
            }
            await self._gateway_policy.replace_quotas(identity.tenant_id, gateway_quotas)
        try:
            await self._repository.upsert_tenant_policy(policy, event)
        except Exception:
            if self._gateway_policy is not None and previous_gateway_quotas is not None:
                await self._gateway_policy.replace_quotas(
                    identity.tenant_id, previous_gateway_quotas
                )
            raise
        return policy

    async def list_outbox(
        self,
        identity: Identity,
        after_sequence: int,
        limit: int,
    ) -> OutboxList:
        """按顺序游标读取租户 Outbox，支持 CDC/Relay 的幂等断点续传。"""
        items, next_cursor = await self._repository.list_outbox(
            identity.tenant_id,
            after_sequence,
            limit,
        )
        return OutboxList(items=items, next_cursor=next_cursor)

    async def _resolution(
        self,
        identity: Identity,
        release: ReleaseManifest,
        environment: str,
        session_id: str,
        assignment: str,
        pinned: bool,
    ) -> RuntimeResolution:
        """将已选择发布转换为 Runtime 解析结果，保留会话绑定与分流来源。"""
        version = await self.get_version(identity, release.agent_id, release.version_id)
        return RuntimeResolution(
            tenant_id=identity.tenant_id,
            agent_id=release.agent_id,
            environment=environment,
            session_id=session_id,
            release_id=release.release_id,
            version_id=version.version_id,
            assignment=assignment,
            pinned=pinned,
            snapshot=version.snapshot,
        )

    @staticmethod
    def _require_platform_super_admin(identity: Identity) -> None:
        """限制全局租户目录操作，不能因 BFF 工作负载附加 agent-admin 而越权。"""
        if "platform-super-admin" not in identity.roles:
            raise ForbiddenError("The platform-super-admin role is required for tenant management.")

    @staticmethod
    def _event(
        event_type: str,
        trace_id: str,
        tenant_id: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> OutboxEvent:
        """构造带 Trace 与聚合键的领域事件，调用方必须与业务写入同事务提交。"""
        return OutboxEvent(
            event_id=f"evt_{uuid4().hex}",
            event_type=event_type,
            trace_id=trace_id,
            tenant_id=tenant_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=utc_now(),
            payload=payload,
        )


def _hash(value: Any) -> str:
    """对规范化 JSON 求哈希，使跨进程的冻结组件变更可被确定性检测。"""
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _component_hashes(spec: dict[str, Any]) -> dict[str, str]:
    """分别计算知识与工具绑定哈希，避免仅靠版本号掩盖组件内容漂移。"""
    return {
        "knowledge": _hash(spec["knowledge"]),
        "tools": _hash(spec["tools"]),
    }


def _bucket(tenant_id: str, agent_id: str, environment: str, session_id: str) -> int:
    """用稳定哈希将同一会话固定分桶，确保灰度期间请求不会随机跳版本。"""
    key = f"{tenant_id}:{agent_id}:{environment}:{session_id}".encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big") % 100

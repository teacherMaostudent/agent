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

from app.application.exceptions import (
    ConflictError,
    DraftValidationError,
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
    TenantPolicy,
    TenantPolicyUpdate,
    ValidationReport,
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

    async def create_agent(
        self,
        identity: Identity,
        request: AgentCreate,
        trace_id: str,
    ) -> AgentDefinition:
        """创建或构建 create_agent 对应的受控业务步骤。


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
        """读取或查询 get_agent 对应的受控业务步骤。


        Return the tenant-scoped record or raise the domain not-found error.
        """
        agent = await self._repository.get_agent(identity.tenant_id, agent_id)
        if not agent:
            raise NotFoundError(f"Agent '{agent_id}' was not found.")
        return agent

    async def list_agents(self, identity: Identity) -> list[AgentDefinition]:
        """读取或查询 list_agents 对应的受控业务步骤。


        List records within the caller tenant without changing release state.
        """
        return await self._repository.list_agents(identity.tenant_id)

    async def update_draft(
        self,
        identity: Identity,
        agent_id: str,
        request: AgentDraftUpdate,
        trace_id: str,
    ) -> AgentDefinition:
        """更新 update_draft 对应的受控业务步骤。


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
        """校验 validate_draft 对应的受控业务步骤。


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
        """发布或投递 publish_version 对应的受控业务步骤。


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

        now = utc_now()
        version_id = f"av_{uuid4().hex}"
        component_hashes = _component_hashes(agent.draft.model_dump(mode="json"))
        snapshot = PublishedSnapshot(
            tenant_id=identity.tenant_id,
            agent_id=agent_id,
            agent_version=f"{agent_id}:{request.semantic_version}",
            graph_version=f"{agent.draft.graph.graph_id}:{agent.revision}",
            prompt_version=f"{agent.draft.prompt.prompt_id}:{agent.revision}",
            knowledge_version=f"kb:{component_hashes['knowledge'][:12]}",
            tool_set_version=f"tools:{component_hashes['tools'][:12]}",
            model_policy_version=f"{agent.draft.model_policy.policy_id}:{agent.revision}",
            spec=agent.draft,
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
                    issues=[{
                        "severity": "error",
                        "code": "runtime_snapshot.compile_failed",
                        "path": "runtime_executor",
                        "message": str(exc),
                    }],
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
        """处理 resolve_runtime 对应的当前组件内部业务步骤。


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

    async def update_tenant_policy(
        self,
        identity: Identity,
        request: TenantPolicyUpdate,
        trace_id: str,
    ) -> TenantPolicy:
        """更新 update_tenant_policy 对应的受控业务步骤。


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
            },
        )
        await self._repository.upsert_tenant_policy(policy, event)
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

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
        tool_catalog_validator=None,
    ) -> None:
        self._repository = repository
        self._governance = governance
        self._require_quality_gate = require_quality_gate
        self._tool_catalog_validator = tool_catalog_validator

    async def create_agent(
        self,
        identity: Identity,
        request: AgentCreate,
        trace_id: str,
    ) -> AgentDefinition:
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
        agent = await self._repository.get_agent(identity.tenant_id, agent_id)
        if not agent:
            raise NotFoundError(f"Agent '{agent_id}' was not found.")
        return agent

    async def list_agents(self, identity: Identity) -> list[AgentDefinition]:
        return await self._repository.list_agents(identity.tenant_id)

    async def update_draft(
        self,
        identity: Identity,
        agent_id: str,
        request: AgentDraftUpdate,
        trace_id: str,
    ) -> AgentDefinition:
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
        await self.get_agent(identity, agent_id)
        return await self._repository.list_versions(identity.tenant_id, agent_id)

    async def get_version(
        self,
        identity: Identity,
        agent_id: str,
        version_id: str,
    ) -> AgentVersion:
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
        version = await self.get_version(identity, agent_id, request.version_id)
        gate: dict[str, Any] = {}
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

    async def list_releases(
        self,
        identity: Identity,
        agent_id: str,
        environment: str | None = None,
    ) -> list[ReleaseManifest]:
        await self.get_agent(identity, agent_id)
        return await self._repository.list_releases(identity.tenant_id, agent_id, environment)

    async def get_release(self, identity: Identity, release_id: str) -> ReleaseManifest:
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
        release = await self.get_release(identity, release_id)
        version = await self.get_version(identity, release.agent_id, release.version_id)
        return version.snapshot

    async def get_tenant_policy(self, identity: Identity) -> TenantPolicy:
        policy = await self._repository.get_tenant_policy(identity.tenant_id)
        return policy or TenantPolicy(tenant_id=identity.tenant_id)

    async def update_tenant_policy(
        self,
        identity: Identity,
        request: TenantPolicyUpdate,
        trace_id: str,
    ) -> TenantPolicy:
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
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _component_hashes(spec: dict[str, Any]) -> dict[str, str]:
    return {
        "knowledge": _hash(spec["knowledge"]),
        "tools": _hash(spec["tools"]),
    }


def _bucket(tenant_id: str, agent_id: str, environment: str, session_id: str) -> int:
    key = f"{tenant_id}:{agent_id}:{environment}:{session_id}".encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big") % 100

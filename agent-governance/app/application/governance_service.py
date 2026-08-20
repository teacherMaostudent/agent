"""Governance event ingestion and tenant-scoped audit operations."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from app.application.exceptions import InvalidStateError, NotFoundError
from app.domain.evaluator import evaluate
from app.domain.models import (
    AuditEvent,
    AuditEventList,
    ComplianceReport,
    Finding,
    FindingList,
    FindingResolution,
    FindingStatus,
    GovernanceEvent,
    Identity,
    IngestionResult,
    Severity,
    TenantPolicy,
    TenantPolicyUpdate,
    utc_now,
)
from app.infrastructure.sqlite_repository import SqliteRepository


class GovernanceService:
    """先持久化不可变审计事件，再暴露派生的租户治理结论。

    Evaluation is deterministic at ingestion time.  A duplicate event does
    not create findings again, preserving idempotency across outbox retries.
    """

    def __init__(self, repository: SqliteRepository) -> None:
        """注入治理仓储与规则评估器；服务负责记录和评估事实，不在事件消费事务中调用在线
        Runtime。
        """
        self._repository = repository

    async def ingest(self, event: GovernanceEvent) -> IngestionResult:
        """按事件 ID 幂等写入审计链；重复投递不会重复生成治理发现项。"""
        policy = await self.get_tenant_policy_for(event.tenant_id)
        findings = evaluate(event, policy)
        audit_event = AuditEvent(**event.model_dump(), sequence=0, received_at=utc_now())
        accepted = await self._repository.ingest(audit_event, findings)
        return IngestionResult(
            accepted=accepted,
            duplicate=not accepted,
            finding_ids=[finding.finding_id for finding in findings] if accepted else [],
        )

    async def list_audit_events(
        self, identity: Identity, after_sequence: int, limit: int
    ) -> AuditEventList:
        """以序列游标读取调用方租户审计链，分页不会跳过或重排历史事件。"""
        items, next_cursor = await self._repository.list_audit_events(
            identity.tenant_id, after_sequence, limit
        )
        return AuditEventList(items=items, next_cursor=next_cursor)

    async def list_findings(
        self, identity: Identity, status: FindingStatus | None, limit: int
    ) -> FindingList:
        """按调用方租户和可选状态读取发现项，避免跨租户的处置可见性。"""
        return FindingList(
            items=await self._repository.list_findings(identity.tenant_id, status, limit)
        )

    async def resolve_finding(
        self, identity: Identity, finding_id: str, request: FindingResolution
    ) -> Finding:
        """原子解决开放发现项；重复解决与不存在的 ID 返回不同领域错误。"""
        finding = await self._repository.resolve_finding(
            identity.tenant_id,
            finding_id,
            identity.user_id,
            request.note,
            utc_now().isoformat(),
        )
        if finding:
            return finding
        if await self._repository.get_finding(identity.tenant_id, finding_id):
            raise InvalidStateError("Finding is already resolved.")
        raise NotFoundError(f"Finding '{finding_id}' was not found.")

    async def get_tenant_policy(self, identity: Identity) -> TenantPolicy:
        """读取审计身份所属租户的策略，不能通过请求参数指定其他租户。"""
        return await self.get_tenant_policy_for(identity.tenant_id)

    async def get_tenant_policy_for(self, tenant_id: str) -> TenantPolicy:
        """加载指定租户策略；未配置时生成独立默认值，不回退到共享策略。"""
        policy = await self._repository.get_tenant_policy(tenant_id)
        return policy or TenantPolicy(tenant_id=tenant_id)

    async def update_tenant_policy(
        self, identity: Identity, request: TenantPolicyUpdate
    ) -> TenantPolicy:
        """更新租户策略并同步追加审计事件，确保策略变更本身可复核。"""
        policy = TenantPolicy(
            tenant_id=identity.tenant_id,
            **request.model_dump(),
            updated_by=identity.user_id,
            updated_at=utc_now(),
        )
        event_time = utc_now()
        await self._repository.upsert_tenant_policy(
            policy,
            AuditEvent(
                event_id=f"evt_{uuid4().hex}",
                source_service="agent-governance",
                event_type="governance.policy.updated",
                trace_id=f"trace_{uuid4().hex}",
                tenant_id=identity.tenant_id,
                occurred_at=event_time,
                received_at=event_time,
                payload={
                    "subject_type": "tenant_policy",
                    "subject_id": identity.tenant_id,
                    "updated_by": identity.user_id,
                },
                sequence=0,
            ),
        )
        return policy

    async def report(
        self, identity: Identity, from_time: datetime | None, to_time: datetime | None
    ) -> ComplianceReport:
        """聚合受限时间窗内的审计与发现项，并按开放严重度推导合规状态。"""
        total, events_by_source, findings = await self._repository.report(
            identity.tenant_id,
            from_time.isoformat() if from_time else None,
            to_time.isoformat() if to_time else None,
        )
        findings_by_severity = {severity: 0 for severity in Severity}
        open_findings = 0
        for finding in findings:
            findings_by_severity[finding.severity] += 1
            if finding.status == FindingStatus.OPEN:
                open_findings += 1
        status = "compliant"
        open_severities = {
            finding.severity for finding in findings if finding.status == FindingStatus.OPEN
        }
        if Severity.CRITICAL in open_severities or Severity.HIGH in open_severities:
            status = "violation"
        elif open_findings:
            status = "attention"
        return ComplianceReport(
            tenant_id=identity.tenant_id,
            from_time=from_time,
            to_time=to_time,
            total_events=total,
            events_by_source=events_by_source,
            findings_by_severity=findings_by_severity,
            open_findings=open_findings,
            compliance_status=status,
        )

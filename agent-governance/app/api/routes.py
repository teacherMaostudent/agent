from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import auditor_identity, get_container, validate_event_key
from app.application.evaluation_service import (
    GOLDEN_CASE,
    JUDGE_RUBRIC,
    PROMPT_VERSION,
    RETRIEVAL_STRATEGY,
)
from app.application.governance_service import GovernanceService
from app.container import AppContainer
from app.domain.models import (
    AuditEventList,
    ComplianceReport,
    Finding,
    FindingList,
    FindingResolution,
    FindingStatus,
    GovernanceEvent,
    HealthStatus,
    Identity,
    IngestionResult,
    TenantPolicy,
    TenantPolicyUpdate,
)

router = APIRouter()
Auditor = Annotated[Identity, Depends(auditor_identity)]
Container = Annotated[AppContainer, Depends(get_container)]
EventKey = Annotated[None, Depends(validate_event_key)]


def service(container: AppContainer) -> GovernanceService:
    return container.service


@router.get("/health/live", response_model=HealthStatus, tags=["health"])
async def liveness() -> HealthStatus:
    return HealthStatus(status="ok")


@router.get("/health/ready", response_model=HealthStatus, tags=["health"])
async def readiness(container: Container) -> HealthStatus:
    await container.repository.healthcheck()
    return HealthStatus(status="ok")


@router.post(
    "/v1/governance/events",
    response_model=IngestionResult,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["ingestion"],
)
async def ingest_event(
    event: GovernanceEvent, _: EventKey, container: Container
) -> IngestionResult:
    # Pydantic validates the service model; the shared JSON Schema additionally
    # protects cross-language publishers from contract drift at this boundary.
    container.schema_registry.validate("governance-event.v1.json", event.model_dump(mode="json"))
    return await service(container).ingest(event)


@router.get("/v1/governance/audit-events", response_model=AuditEventList, tags=["audit"])
async def list_audit_events(
    identity: Auditor,
    container: Container,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1_000),
) -> AuditEventList:
    return await service(container).list_audit_events(identity, after_sequence, limit)


@router.get("/v1/governance/audit-events/verify", tags=["audit"])
async def verify_audit_chain(identity: Auditor, container: Container) -> dict[str, Any]:
    return await container.repository.verify_audit_chain(identity.tenant_id)


@router.get("/v1/governance/findings", response_model=FindingList, tags=["findings"])
async def list_findings(
    identity: Auditor,
    container: Container,
    finding_status: Annotated[FindingStatus | None, Query(alias="status")] = None,
    limit: int = Query(default=100, ge=1, le=1_000),
) -> FindingList:
    return await service(container).list_findings(identity, finding_status, limit)


@router.post(
    "/v1/governance/findings/{finding_id}/resolve", response_model=Finding, tags=["findings"]
)
async def resolve_finding(
    finding_id: str,
    request: FindingResolution,
    identity: Auditor,
    container: Container,
) -> Finding:
    return await service(container).resolve_finding(identity, finding_id, request)


@router.get("/v1/governance/tenant-policy", response_model=TenantPolicy, tags=["policy"])
async def get_tenant_policy(identity: Auditor, container: Container) -> TenantPolicy:
    return await service(container).get_tenant_policy(identity)


@router.put("/v1/governance/tenant-policy", response_model=TenantPolicy, tags=["policy"])
async def update_tenant_policy(
    request: TenantPolicyUpdate, identity: Auditor, container: Container
) -> TenantPolicy:
    return await service(container).update_tenant_policy(identity, request)


@router.get("/v1/governance/reports/compliance", response_model=ComplianceReport, tags=["reports"])
async def compliance_report(
    identity: Auditor,
    container: Container,
    from_time: Annotated[datetime | None, Query()] = None,
    to_time: Annotated[datetime | None, Query()] = None,
) -> ComplianceReport:
    return await service(container).report(identity, from_time, to_time)


# Canonical Governance-owned evaluation APIs. Gateway keeps the former
# /admin/eval routes as a compatibility proxy to these endpoints.
@router.get("/v1/governance/evaluations", tags=["evaluation"])
async def evaluation_snapshot(identity: Auditor, container: Container) -> dict[str, Any]:
    return await container.evaluation.snapshot(identity.tenant_id)


@router.put("/v1/governance/evaluations/prompt-versions", tags=["evaluation"])
async def upsert_prompt_version(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    return await container.evaluation.upsert_asset(identity.tenant_id, PROMPT_VERSION, request)


@router.put("/v1/governance/evaluations/retrieval-strategies", tags=["evaluation"])
async def upsert_retrieval_strategy(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    return await container.evaluation.upsert_asset(identity.tenant_id, RETRIEVAL_STRATEGY, request)


@router.put("/v1/governance/evaluations/golden-dataset", tags=["evaluation"])
async def upsert_golden_case(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    return await container.evaluation.upsert_asset(identity.tenant_id, GOLDEN_CASE, request)


@router.put("/v1/governance/evaluations/judge-rubrics", tags=["evaluation"])
async def upsert_judge_rubric(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    return await container.evaluation.upsert_asset(identity.tenant_id, JUDGE_RUBRIC, request)


@router.post("/v1/governance/evaluations/regression-runs", tags=["evaluation"])
async def run_regression(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    return await container.evaluation.run_regression(identity.tenant_id, request)


@router.post("/v1/governance/evaluations/judge-runs", tags=["evaluation"])
async def run_judge(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    return await container.evaluation.judge(identity.tenant_id, identity.user_id, request)


@router.post("/v1/governance/evaluations/judge-runs/{run_id}/calibration", tags=["evaluation"])
async def calibrate_judge(run_id: str, identity: Auditor, container: Container) -> dict[str, Any]:
    return await container.evaluation.calibrate(identity.tenant_id, run_id)


@router.get("/v1/governance/evaluations/calibration/weekly-report", tags=["evaluation"])
async def weekly_calibration_report(identity: Auditor, container: Container) -> dict[str, Any]:
    return await container.evaluation.weekly_calibration_report(identity.tenant_id)


@router.post(
    "/v1/governance/evaluations/judge-runs/{run_id}/quality-gate",
    tags=["evaluation"],
)
async def run_quality_gate(
    run_id: str,
    identity: Auditor,
    container: Container,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await container.evaluation.quality_gate(identity.tenant_id, run_id, request)


@router.post("/v1/governance/evaluations/traces", tags=["evaluation"])
async def record_evaluation_trace(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    return await container.evaluation.record_trace(identity.tenant_id, request)


@router.post("/v1/governance/compliance/reviews", tags=["compliance-workflow"])
async def create_compliance_review(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    return await container.compliance.create(identity.tenant_id, identity.user_id, request)


@router.get("/v1/governance/compliance", tags=["compliance-workflow"])
async def compliance_workflow_snapshot(identity: Auditor, container: Container) -> dict[str, Any]:
    return await container.compliance.snapshot(identity.tenant_id)


@router.get("/v1/governance/compliance/reviews", tags=["compliance-workflow"])
async def list_compliance_reviews(identity: Auditor, container: Container) -> list[dict[str, Any]]:
    return await container.compliance.list(identity.tenant_id)


@router.get("/v1/governance/compliance/reviews/{review_id}", tags=["compliance-workflow"])
async def get_compliance_review(
    review_id: str, identity: Auditor, container: Container
) -> dict[str, Any]:
    return await container.compliance.get(identity.tenant_id, review_id)


@router.post(
    "/v1/governance/compliance/reviews/{review_id}/confirm",
    tags=["compliance-workflow"],
)
async def confirm_compliance_review(
    review_id: str,
    request: dict[str, Any],
    identity: Auditor,
    container: Container,
) -> dict[str, Any]:
    return await container.compliance.confirm(identity.tenant_id, review_id, request)


@router.get("/v1/governance/compliance/audit-logs", tags=["compliance-workflow"])
async def compliance_audit_logs(identity: Auditor, container: Container) -> list[dict[str, Any]]:
    return await container.compliance.audit_logs(identity.tenant_id)


@router.post("/v1/governance/evaluations/traces/gateway", tags=["online-evaluation"])
async def record_gateway_trace(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    return await container.evaluation.record_gateway_trace(identity.tenant_id, request)


@router.post("/v1/governance/evaluations/feedback", tags=["online-evaluation"])
async def record_feedback(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    return await container.evaluation.record_feedback(identity.tenant_id, identity.user_id, request)


@router.get("/v1/governance/evaluations/online", tags=["online-evaluation"])
async def online_evaluation_snapshot(identity: Auditor, container: Container) -> dict[str, Any]:
    return await container.evaluation.online_snapshot(identity.tenant_id)


@router.post(
    "/v1/governance/evaluations/online/samples/{sample_id}/judge",
    tags=["online-evaluation"],
)
async def judge_online_sample(
    sample_id: str, identity: Auditor, container: Container
) -> dict[str, Any]:
    return await container.evaluation.judge_online(identity.tenant_id, identity.user_id, sample_id)


@router.post(
    "/v1/governance/evaluations/online/samples/{sample_id}/review",
    tags=["online-evaluation"],
)
async def review_online_sample(
    sample_id: str,
    request: dict[str, Any],
    identity: Auditor,
    container: Container,
) -> dict[str, Any]:
    return await container.evaluation.review_online_sample(
        identity.tenant_id, identity.user_id, sample_id, request
    )


@router.post(
    "/v1/governance/evaluations/online/golden-candidates/{candidate_id}/review",
    tags=["online-evaluation"],
)
async def review_golden_candidate(
    candidate_id: str,
    request: dict[str, Any],
    identity: Auditor,
    container: Container,
) -> dict[str, Any]:
    return await container.evaluation.review_golden_candidate(
        identity.tenant_id, identity.user_id, candidate_id, request
    )

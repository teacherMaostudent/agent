from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

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
    """返回治理应用服务，路由层不直接实现审计一致性或规则评估。"""
    return container.service


@router.get("/health/live", response_model=HealthStatus, tags=["health"])
async def liveness() -> HealthStatus:
    """报告进程存活；不声明数据库、Kafka 或对象存储已经可用。"""
    return HealthStatus(status="ok")


@router.get("/health/ready", response_model=HealthStatus, tags=["health"])
async def readiness(container: Container) -> HealthStatus:
    """确认审计仓储可访问；异常会由统一错误处理映射为非就绪响应。"""
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
    """校验共享事件 Schema 后幂等摄取审计事件，调用方须持有写入密钥。"""
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
    trace_id: str | None = Query(default=None, min_length=1, max_length=160),
) -> AuditEventList:
    """以序号分页读取租户审计链，审计身份不能跨越自己的租户边界。"""
    return await service(container).list_audit_events(
        identity, after_sequence, limit, trace_id=trace_id
    )


@router.get("/internal/v1/governance/audit-events/runs/{run_id}", tags=["internal"])
async def list_run_audit_events_for_runtime(
    run_id: str,
    _: EventKey,
    container: Container,
    x_tenant_id: str = Header(alias="X-Tenant-Id"),
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=1_000, ge=1, le=1_000),
) -> dict[str, Any]:
    """只向 Runtime 返回单个 Run 的审计事实；Runtime 仍须校验该 Run 的用户所有权。"""
    events = await service(container).list_audit_events(
        Identity(tenant_id=x_tenant_id, user_id="agent-runtime", roles={"service"}),
        after_sequence, limit, run_id=run_id,
    )
    items = [
        item.model_dump(mode="json")
        for item in events.items
    ]
    return {"items": items, "next_cursor": events.next_cursor}


@router.get("/v1/governance/audit-events/verify", tags=["audit"])
async def verify_audit_chain(identity: Auditor, container: Container) -> dict[str, Any]:
    """验证指定租户哈希链完整性，用于审计导出前的防篡改检查。"""
    return await container.repository.verify_audit_chain(identity.tenant_id)


@router.post(
    "/v1/governance/audit-exports",
    status_code=status.HTTP_202_ACCEPTED,
    tags=["audit-export"],
)
async def create_audit_export(identity: Auditor, container: Container) -> dict[str, Any]:
    """持久化异步 WORM 导出请求，不在 HTTP 请求内阻塞等待 KMS 或对象存储。"""
    return await container.worm_exports.create(identity.tenant_id, identity.user_id)


@router.get("/v1/governance/audit-exports", tags=["audit-export"])
async def list_audit_exports(
    identity: Auditor,
    container: Container,
    limit: int = Query(default=100, ge=1, le=1_000),
) -> dict[str, Any]:
    """列出租户导出的进度、保留证明与受限失败详情，不暴露其他租户作业。"""
    return {"items": await container.worm_exports.list(identity.tenant_id, limit)}


@router.get("/v1/governance/audit-exports/{job_id}", tags=["audit-export"])
async def get_audit_export(
    job_id: str, identity: Auditor, container: Container
) -> dict[str, Any]:
    """返回单个租户隔离的导出作业；对象内容不经 Web 服务反向代理。"""
    job = await container.worm_exports.get(identity.tenant_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="audit export job not found")
    return job


@router.post(
    "/v1/governance/audit-exports/{job_id}/requeue",
    status_code=status.HTTP_202_ACCEPTED,
    tags=["audit-export"],
)
async def requeue_audit_export(
    job_id: str, identity: Auditor, container: Container
) -> dict[str, Any]:
    """仅在审计员显式操作下重新排队失败导出，避免 Worker 自动绕过人工复核。"""
    try:
        job = await container.worm_exports.requeue(identity.tenant_id, job_id, identity.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not job:
        raise HTTPException(status_code=404, detail="audit export job not found")
    return job


@router.get("/v1/governance/findings", response_model=FindingList, tags=["findings"])
async def list_findings(
    identity: Auditor,
    container: Container,
    finding_status: Annotated[FindingStatus | None, Query(alias="status")] = None,
    limit: int = Query(default=100, ge=1, le=1_000),
) -> FindingList:
    """按状态列出规则发现项；不允许以 API 过滤条件扩大可见租户范围。"""
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
    """记录人工处置结论；服务层拒绝不合法的发现项状态回退。"""
    return await service(container).resolve_finding(identity, finding_id, request)


@router.get("/v1/governance/tenant-policy", response_model=TenantPolicy, tags=["policy"])
async def get_tenant_policy(identity: Auditor, container: Container) -> TenantPolicy:
    """获取本租户治理策略，策略是评估与审计判断的唯一配置来源。"""
    return await service(container).get_tenant_policy(identity)


@router.put("/v1/governance/tenant-policy", response_model=TenantPolicy, tags=["policy"])
async def update_tenant_policy(
    request: TenantPolicyUpdate, identity: Auditor, container: Container
) -> TenantPolicy:
    """更新租户治理策略；服务层同时记录可追溯审计事件。"""
    return await service(container).update_tenant_policy(identity, request)


@router.get("/v1/governance/reports/compliance", response_model=ComplianceReport, tags=["reports"])
async def compliance_report(
    identity: Auditor,
    container: Container,
    from_time: Annotated[datetime | None, Query()] = None,
    to_time: Annotated[datetime | None, Query()] = None,
) -> ComplianceReport:
    """生成时间范围内的租户合规报告，时间过滤不改变审计事件原文。"""
    return await service(container).report(identity, from_time, to_time)


# Canonical Governance-owned evaluation APIs. Gateway keeps the former
# /admin/eval routes as a compatibility proxy to these endpoints.
@router.get("/v1/governance/evaluations", tags=["evaluation"])
async def evaluation_snapshot(identity: Auditor, container: Container) -> dict[str, Any]:
    """返回治理侧评测资产与运行概览，而非 Gateway 的兼容代理数据。"""
    return await container.evaluation.snapshot(identity.tenant_id)


@router.put("/v1/governance/evaluations/prompt-versions", tags=["evaluation"])
async def upsert_prompt_version(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    """登记不可变 Prompt 版本；Judge 运行只能引用已登记的版本。"""
    return await container.evaluation.upsert_asset(identity.tenant_id, PROMPT_VERSION, request)


@router.put("/v1/governance/evaluations/retrieval-strategies", tags=["evaluation"])
async def upsert_retrieval_strategy(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    """登记可复现检索策略，以固定评测中检索配置的语义。"""
    return await container.evaluation.upsert_asset(identity.tenant_id, RETRIEVAL_STRATEGY, request)


@router.put("/v1/governance/evaluations/golden-dataset", tags=["evaluation"])
async def upsert_golden_case(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    """登记 Golden Case 及专家标签、证据预期，作为发布门禁基准。"""
    return await container.evaluation.upsert_asset(identity.tenant_id, GOLDEN_CASE, request)


@router.put("/v1/governance/evaluations/judge-rubrics", tags=["evaluation"])
async def upsert_judge_rubric(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    """登记 Judge 量表版本，冻结模型、Prompt 和评分规则的组合。"""
    return await container.evaluation.upsert_asset(identity.tenant_id, JUDGE_RUBRIC, request)


@router.post("/v1/governance/evaluations/regression-runs", tags=["evaluation"])
async def run_regression(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    """运行回归评测，结果与输入资产版本绑定，避免均分掩盖配置漂移。"""
    return await container.evaluation.run_regression(identity.tenant_id, request)


@router.post("/v1/governance/evaluations/judge-runs", tags=["evaluation"])
async def run_judge(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    """执行冻结 Judge 运行；调用方身份会作为评测审计责任人保存。"""
    return await container.evaluation.judge(identity.tenant_id, identity.user_id, request)


@router.post("/v1/governance/evaluations/knowledge-change-gates", tags=["evaluation"])
async def request_knowledge_change_gate(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    """Create a pending regression gate after an approved Wiki page is reindexed."""
    return await container.evaluation.request_knowledge_change_gate(
        identity.tenant_id, identity.user_id, request
    )


@router.post("/v1/governance/evaluations/retrieval-shadow", tags=["evaluation"])
async def record_retrieval_shadow(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    """Record an internal Retrieval Release comparison before any user Canary routing."""

    return await container.evaluation.record_retrieval_shadow(identity.tenant_id, request)


@router.post("/v1/governance/evaluations/judge-runs/{run_id}/calibration", tags=["evaluation"])
async def calibrate_judge(run_id: str, identity: Auditor, container: Container) -> dict[str, Any]:
    """以专家标注集校准 Judge，未通过校准的版本不得用于质量门禁。"""
    return await container.evaluation.calibrate(identity.tenant_id, run_id)


@router.get("/v1/governance/evaluations/calibration/weekly-report", tags=["evaluation"])
async def weekly_calibration_report(identity: Auditor, container: Container) -> dict[str, Any]:
    """汇总本租户按周 Judge 校准与漂移信号，供治理负责人复核。"""
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
    """依据分组 Hard Gate 评估一次 Judge 运行，禁止仅用平均分放行。"""
    return await container.evaluation.quality_gate(identity.tenant_id, run_id, request)


@router.post("/v1/governance/evaluations/traces", tags=["evaluation"])
async def record_evaluation_trace(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    """写入离线评测 Trace，使答案、证据和指标能按版本追溯。"""
    return await container.evaluation.record_trace(identity.tenant_id, request)


@router.post("/v1/governance/compliance/reviews", tags=["compliance-workflow"])
async def create_compliance_review(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    """创建需人工确认的合规工作项；审批状态由 Governance 独立持有。"""
    return await container.compliance.create(identity.tenant_id, identity.user_id, request)


@router.get("/v1/governance/compliance", tags=["compliance-workflow"])
async def compliance_workflow_snapshot(identity: Auditor, container: Container) -> dict[str, Any]:
    """返回租户合规工作流概览，不执行或推进任何业务操作。"""
    return await container.compliance.snapshot(identity.tenant_id)


@router.get("/v1/governance/compliance/reviews", tags=["compliance-workflow"])
async def list_compliance_reviews(identity: Auditor, container: Container) -> list[dict[str, Any]]:
    """列出租户合规复核项，供授权审计人员完成处置。"""
    return await container.compliance.list(identity.tenant_id)


@router.get("/v1/governance/compliance/reviews/{review_id}", tags=["compliance-workflow"])
async def get_compliance_review(
    review_id: str, identity: Auditor, container: Container
) -> dict[str, Any]:
    """按租户读取单个合规复核项，避免通过全局 ID 越权读取。"""
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
    """确认合规复核；服务层保证确认只能发生一次并留下审计记录。"""
    return await container.compliance.confirm(identity.tenant_id, review_id, request)


@router.get("/v1/governance/compliance/audit-logs", tags=["compliance-workflow"])
async def compliance_audit_logs(identity: Auditor, container: Container) -> list[dict[str, Any]]:
    """读取合规工作流自身的审计日志，和业务审计事件分开建模。"""
    return await container.compliance.audit_logs(identity.tenant_id)


@router.post("/v1/governance/evaluations/traces/gateway", tags=["online-evaluation"])
async def record_gateway_trace(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    """记录 Gateway 线上调用 Trace，后续抽样、脱敏与保留策略在服务层执行。"""
    return await container.evaluation.record_gateway_trace(identity.tenant_id, request)


@router.post("/v1/governance/evaluations/feedback", tags=["online-evaluation"])
async def record_feedback(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    """记录人工反馈及责任人，用于线上校准闭环而非直接改写 Golden 集。"""
    return await container.evaluation.record_feedback(identity.tenant_id, identity.user_id, request)


@router.get("/v1/governance/evaluations/online", tags=["online-evaluation"])
async def online_evaluation_snapshot(identity: Auditor, container: Container) -> dict[str, Any]:
    """返回线上抽样和人工复核闭环的租户级状态。"""
    return await container.evaluation.online_snapshot(identity.tenant_id)


@router.post("/v1/governance/evaluations/online/gate", tags=["online-evaluation"])
async def evaluate_online_gate(
    request: dict[str, Any], identity: Auditor, container: Container
) -> dict[str, Any]:
    """根据冻结 Release/Snapshot 的线上窗口计算 HOLD、PAUSE、ROLLBACK 或 PROMOTE。"""
    return await container.evaluation.online_gate(identity.tenant_id, request)


@router.get("/internal/v1/governance/gate-decisions/{decision_id}", tags=["internal"])
async def get_gate_decision(
    decision_id: str, identity: Auditor, container: Container
) -> dict[str, Any]:
    """向受认证 Release Controller 提供已保存的治理结论，不接受调用方伪造指标。"""
    return await container.evaluation.get_gate_decision(identity.tenant_id, decision_id)


@router.post(
    "/v1/governance/evaluations/online/samples/{sample_id}/judge",
    tags=["online-evaluation"],
)
async def judge_online_sample(
    sample_id: str, identity: Auditor, container: Container
) -> dict[str, Any]:
    """使用冻结 Judge 审核一个线上样本，结果不会替代人工最终复核。"""
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
    """记录人工对线上样本的复核，作为后续专家校准数据来源。"""
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
    """审核候选 Golden 样本；只有通过复核的记录才能进入专家标注集。"""
    return await container.evaluation.review_golden_candidate(
        identity.tenant_id, identity.user_id, candidate_id, request
    )


@router.get(
    "/v1/governance/evaluations/online/golden-candidates",
    tags=["online-evaluation"],
)
async def list_golden_candidates(
    identity: Auditor, container: Container, limit: int = Query(default=100, ge=1, le=200)
) -> dict[str, Any]:
    """列出候选 Golden 的最小审核投影；不返回线上样本原始请求/响应。"""
    return {
        "items": await container.evaluation.list_golden_candidates(
            identity.tenant_id, limit=limit
        )
    }

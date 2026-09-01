"""Control Plane HTTP surface; handlers delegate state semantics to services."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from platform_sdk.contracts.skills import SkillCard

from app.api.dependencies import (
    get_container,
    get_trace_id,
    management_identity,
    runtime_identity,
)
from app.application.control_plane_service import ControlPlaneService
from app.container import AppContainer
from app.domain.models import (
    AgentCreate,
    AgentDefinition,
    AgentDraftUpdate,
    AgentVersion,
    AgentVersionPublish,
    HealthStatus,
    GovernanceReleaseAction,
    Identity,
    OutboxList,
    PublishedSnapshot,
    ReleaseCreate,
    ReleaseManifest,
    ReleasePromote,
    RuntimeResolution,
    SkillCreate,
    SkillDefinition,
    SkillDraftUpdate,
    SkillRuntimeResolution,
    SkillStatusUpdate,
    SkillVersion,
    SkillVersionPublish,
    Tenant,
    TenantCreate,
    TenantPolicy,
    TenantPolicyUpdate,
    TenantUpdate,
    ToolDefinition,
    ToolDraftCreate,
    ToolDraftUpdate,
    ToolReviewCreate,
    ToolVersion,
    ToolVersionPublish,
    ToolVersionStatusUpdate,
    ValidationReport,
    WorkflowCreate,
    WorkflowDefinition,
    WorkflowDraftUpdate,
    WorkflowRelease,
    WorkflowReleaseCreate,
    WorkflowRuntimeResolution,
    WorkflowVersion,
    WorkflowVersionPublish,
)

ManagementIdentity = Annotated[Identity, Depends(management_identity)]
RuntimeIdentity = Annotated[Identity, Depends(runtime_identity)]
Container = Annotated[AppContainer, Depends(get_container)]
TraceId = Annotated[str, Depends(get_trace_id)]

router = APIRouter()


def service(container: AppContainer) -> ControlPlaneService:
    """返回请求生命周期内的应用服务，避免路由层复制发布状态机规则。"""
    return container.service


@router.post(
    "/v1/workflows", response_model=WorkflowDefinition, status_code=201, tags=["workflows"]
)
async def create_workflow(
    request: WorkflowCreate, identity: ManagementIdentity, container: Container, trace_id: TraceId
) -> WorkflowDefinition:
    """创建独立 Workflow Draft。"""
    return await service(container).create_workflow(identity, request, trace_id)


@router.get("/v1/workflows/{workflow_id}", response_model=WorkflowDefinition, tags=["workflows"])
async def get_workflow(
    workflow_id: str, identity: ManagementIdentity, container: Container
) -> WorkflowDefinition:
    """读取租户范围内的 Workflow Draft。"""
    return await service(container).get_workflow(identity, workflow_id)


@router.put(
    "/v1/workflows/{workflow_id}/draft", response_model=WorkflowDefinition, tags=["workflows"]
)
async def update_workflow(
    workflow_id: str,
    request: WorkflowDraftUpdate,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> WorkflowDefinition:
    """通过 CAS 更新 Workflow Draft。"""
    return await service(container).update_workflow_draft(identity, workflow_id, request, trace_id)


@router.post(
    "/v1/workflows/{workflow_id}/versions",
    response_model=WorkflowVersion,
    status_code=201,
    tags=["workflows"],
)
async def publish_workflow(
    workflow_id: str,
    request: WorkflowVersionPublish,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> WorkflowVersion:
    """冻结 WorkflowVersion 与执行计划。"""
    return await service(container).publish_workflow_version(
        identity, workflow_id, request, trace_id
    )


@router.post(
    "/v1/workflows/{workflow_id}/releases",
    response_model=WorkflowRelease,
    status_code=201,
    tags=["workflows"],
)
async def release_workflow(
    workflow_id: str,
    request: WorkflowReleaseCreate,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> WorkflowRelease:
    """激活目标环境的 WorkflowVersion。"""
    return await service(container).create_workflow_release(
        identity, workflow_id, request, trace_id
    )


@router.get(
    "/internal/v1/workflows/{workflow_id}/resolve",
    response_model=WorkflowRuntimeResolution,
    tags=["runtime"],
)
async def resolve_workflow(
    workflow_id: str, environment: str, identity: RuntimeIdentity, container: Container
) -> WorkflowRuntimeResolution:
    """供工作负载身份解析冻结 Workflow Release。"""
    return await service(container).resolve_workflow(identity, workflow_id, environment)


@router.get("/health/live", response_model=HealthStatus, tags=["health"])
async def liveness() -> HealthStatus:
    """报告进程存活；不访问数据库，因此不能代表依赖项已就绪。"""
    return HealthStatus(status="ok")


@router.get("/health/ready", response_model=HealthStatus, tags=["health"])
async def readiness(container: Container, response: Response) -> HealthStatus:
    """检查持久化依赖；失败时返回 503，供编排器停止向实例分流。"""
    if not await container.repository.healthcheck():
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthStatus(status="ok")


@router.get("/internal/v1/tool-catalog/runtime-projection", tags=["runtime"])
async def get_tool_runtime_projection(
    identity: RuntimeIdentity, container: Container
) -> dict[str, Any]:
    """向已认证 Gateway 分发只读 Tool Runtime Projection，而不是管理面 Draft。"""
    del identity
    return await container.tool_runtime_projection.current()


@router.post(
    "/v1/tools", response_model=ToolDefinition, status_code=status.HTTP_201_CREATED, tags=["tools"]
)
async def create_tool(
    request: ToolDraftCreate, identity: ManagementIdentity, container: Container, trace_id: TraceId
) -> ToolDefinition:
    """创建工具管理面 Draft；它尚未对 Tool Gateway 或 Agent 可见。"""
    return await service(container).create_tool(identity, request, trace_id)


@router.get("/v1/tools/{tool_id}", response_model=ToolDefinition, tags=["tools"])
async def get_tool(
    tool_id: str, identity: ManagementIdentity, container: Container
) -> ToolDefinition:
    """读取当前租户的工具 Draft。"""
    return await service(container).get_tool(identity, tool_id)


@router.put("/v1/tools/{tool_id}/draft", response_model=ToolDefinition, tags=["tools"])
async def update_tool_draft(
    tool_id: str,
    request: ToolDraftUpdate,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> ToolDefinition:
    """按 revision CAS 更新工具 Draft，不能影响已发布版本。"""
    return await service(container).update_tool_draft(identity, tool_id, request, trace_id)


@router.post(
    "/v1/tools/{tool_id}/versions",
    response_model=ToolVersion,
    status_code=status.HTTP_201_CREATED,
    tags=["tools"],
)
async def publish_tool_version(
    tool_id: str,
    request: ToolVersionPublish,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> ToolVersion:
    """冻结 Draft 为不可变 Candidate Version。"""
    return await service(container).publish_tool_version(identity, tool_id, request, trace_id)


@router.get("/v1/tools/{tool_id}/versions", response_model=list[ToolVersion], tags=["tools"])
async def list_tool_versions(
    tool_id: str, identity: ManagementIdentity, container: Container
) -> list[ToolVersion]:
    """列出可用于审核与发布选择的版本历史。"""
    return await service(container).list_tool_versions(identity, tool_id)


@router.post(
    "/v1/tools/{tool_id}/versions/{version_id}/review", response_model=ToolVersion, tags=["tools"]
)
async def review_tool_version(
    tool_id: str,
    version_id: str,
    request: ToolReviewCreate,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> ToolVersion:
    """记录独立审核结论；发布只能接受 Approved 版本。"""
    return await service(container).review_tool_version(
        identity, tool_id, version_id, request, trace_id
    )


@router.post(
    "/v1/tools/{tool_id}/versions/{version_id}/release", response_model=ToolVersion, tags=["tools"]
)
async def release_tool_version(
    tool_id: str,
    version_id: str,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> ToolVersion:
    """生成唯一 Active Runtime Snapshot，供 Gateway 的只读投影消费。"""
    return await service(container).release_tool_version(identity, tool_id, version_id, trace_id)


@router.post(
    "/v1/tools/{tool_id}/versions/{version_id}/status", response_model=ToolVersion, tags=["tools"]
)
async def update_tool_version_status(
    tool_id: str,
    version_id: str,
    request: ToolVersionStatusUpdate,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> ToolVersion:
    """弃用或退役已发布工具，退役会撤销运行时发布。"""
    return await service(container).update_tool_version_status(
        identity, tool_id, version_id, request, trace_id
    )


@router.post(
    "/v1/skills",
    response_model=SkillDefinition,
    status_code=status.HTTP_201_CREATED,
    tags=["skills"],
)
async def create_skill(
    request: SkillCreate, identity: ManagementIdentity, container: Container, trace_id: TraceId
) -> SkillDefinition:
    """创建可编辑 Skill 草稿；它尚未被 Runtime 或 Capability
    Resolver 使用。
    """
    return await service(container).create_skill(identity, request, trace_id)


@router.get("/v1/skills/catalog", response_model=list[SkillCard], tags=["skills"])
async def list_skill_cards(
    identity: ManagementIdentity,
    container: Container,
    capability_id: str = Query(default="", max_length=160),
) -> list[SkillCard]:
    """渐进披露 Skill Card，不向 Planner/用户泄露完整 Prompt
    和工具绑定。
    """
    return await service(container).list_skill_cards(identity, capability_id)


@router.get("/v1/skills/{skill_id}", response_model=SkillDefinition, tags=["skills"])
async def get_skill(
    skill_id: str, identity: ManagementIdentity, container: Container
) -> SkillDefinition:
    """读取当前租户 Skill 草稿，不能替代冻结版本。"""
    return await service(container).get_skill(identity, skill_id)


@router.put("/v1/skills/{skill_id}/draft", response_model=SkillDefinition, tags=["skills"])
async def update_skill_draft(
    skill_id: str,
    request: SkillDraftUpdate,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> SkillDefinition:
    """通过乐观锁更新 Skill 草稿，发布版本不受影响。"""
    return await service(container).update_skill_draft(identity, skill_id, request, trace_id)


@router.post(
    "/v1/skills/{skill_id}/versions",
    response_model=SkillVersion,
    status_code=status.HTTP_201_CREATED,
    tags=["skills"],
)
async def publish_skill_version(
    skill_id: str,
    request: SkillVersionPublish,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> SkillVersion:
    """编译并冻结 SkillVersion，供 Agent/Workflow
    以摘要精确绑定。
    """
    return await service(container).publish_skill_version(identity, skill_id, request, trace_id)


@router.post(
    "/v1/skills/{skill_id}/versions/{version_id}/status",
    response_model=SkillVersion,
    tags=["skills"],
)
async def update_skill_status(
    skill_id: str,
    version_id: str,
    request: SkillStatusUpdate,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> SkillVersion:
    """变更冻结工件的可用状态，不允许修改其内容或摘要。"""
    return await service(container).update_skill_status(
        identity, skill_id, version_id, request, trace_id
    )


@router.get(
    "/internal/v1/skills/{skill_id}/versions/{version}/resolve",
    response_model=SkillRuntimeResolution,
    tags=["runtime"],
)
async def resolve_skill(
    skill_id: str, version: str, identity: RuntimeIdentity, container: Container
) -> SkillRuntimeResolution:
    """供 Runtime 与 Agent Lab 解析 Active
    SkillVersion。
    """
    return await service(container).resolve_skill(identity, skill_id, version)


@router.post(
    "/v1/agents",
    response_model=AgentDefinition,
    status_code=status.HTTP_201_CREATED,
    tags=["agents"],
)
async def create_agent(
    request: AgentCreate,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> AgentDefinition:
    """创建Agent 定义的 HTTP
    入口：先从已验证身份构造租户上下文，再调用应用服务；路由层不复制授权或状态机逻辑。

    Create a tenant-scoped mutable draft; it is not Runtime-executable yet.
    """
    return await service(container).create_agent(identity, request, trace_id)


@router.get("/v1/agents", response_model=list[AgentDefinition], tags=["agents"])
async def list_agents(
    identity: ManagementIdentity,
    container: Container,
) -> list[AgentDefinition]:
    """仅列出管理身份所属租户的草稿，租户隔离由服务层再次保证。"""
    return await service(container).list_agents(identity)


@router.get("/v1/agents/catalog", tags=["agents"])
async def list_agent_catalog_page(
    identity: ManagementIdentity,
    container: Container,
    limit: int = Query(default=8, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=125_000),
) -> dict[str, Any]:
    """分页返回发布目录元数据，避免 Console 为展示目录传输全量 Agent Draft。

    该端点只投影 Agent ID、Draft 修订和更新时间；版本及 Release 摘要由
    明确的子资源接口单独读取，防止把可执行快照混入目录页。
    """
    items, total_items = await service(container).list_agent_page(
        identity, limit=limit, offset=offset
    )
    return {
        "items": [
            {
                "agent_id": item.agent_id,
                "revision": item.revision,
                "updated_at": item.updated_at,
            }
            for item in items
        ],
        "total_items": total_items,
        "limit": limit,
        "offset": offset,
    }


@router.get("/v1/agents/{agent_id}", response_model=AgentDefinition, tags=["agents"])
async def get_agent(
    agent_id: str,
    identity: ManagementIdentity,
    container: Container,
) -> AgentDefinition:
    """读取一个可变草稿；发布快照不经此接口暴露给普通管理调用方。"""
    return await service(container).get_agent(identity, agent_id)


@router.put("/v1/agents/{agent_id}/draft", response_model=AgentDefinition, tags=["agents"])
async def update_agent_draft(
    agent_id: str,
    request: AgentDraftUpdate,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> AgentDefinition:
    """根据调用方提供的修订号执行乐观并发草稿更新。

    Apply an optimistic-concurrency draft update using the supplied revision.
    """
    return await service(container).update_draft(identity, agent_id, request, trace_id)


@router.post(
    "/v1/agents/{agent_id}/validate",
    response_model=ValidationReport,
    tags=["versions"],
)
async def validate_agent_draft(
    agent_id: str,
    identity: ManagementIdentity,
    container: Container,
) -> ValidationReport:
    """校验草稿在发布前的确定性规则，并返回可供修正的验证结果。

    Return deterministic validation findings without publishing the draft.
    """
    return await service(container).validate_draft(identity, agent_id)


@router.post(
    "/v1/agents/{agent_id}/versions",
    response_model=AgentVersion,
    status_code=status.HTTP_201_CREATED,
    tags=["versions"],
)
async def publish_agent_version(
    agent_id: str,
    request: AgentVersionPublish,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> AgentVersion:
    """将已校验草稿冻结为不可变版本，并写入同事务 Outbox 审计事件。"""
    return await service(container).publish_version(identity, agent_id, request, trace_id)


@router.get(
    "/v1/agents/{agent_id}/versions",
    response_model=list[AgentVersion],
    tags=["versions"],
)
async def list_agent_versions(
    agent_id: str,
    identity: ManagementIdentity,
    container: Container,
) -> list[AgentVersion]:
    """列出指定 Agent 的不可变版本，不改变发布或流量状态。"""
    return await service(container).list_versions(identity, agent_id)


@router.get(
    "/v1/agents/{agent_id}/versions/{version_id}",
    response_model=AgentVersion,
    tags=["versions"],
)
async def get_agent_version(
    agent_id: str,
    version_id: str,
    identity: ManagementIdentity,
    container: Container,
) -> AgentVersion:
    """按租户和 Agent 双重范围读取版本，防止跨 Agent 的版本枚举。"""
    return await service(container).get_version(identity, agent_id, version_id)


@router.post(
    "/v1/agents/{agent_id}/releases",
    response_model=ReleaseManifest,
    status_code=status.HTTP_201_CREATED,
    tags=["releases"],
)
async def create_release(
    agent_id: str,
    request: ReleaseCreate,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> ReleaseManifest:
    """由冻结版本创建候选发布清单；尚未提升前 Runtime 不会选择它。"""
    return await service(container).create_release(identity, agent_id, request, trace_id)


@router.get(
    "/v1/agents/{agent_id}/releases",
    response_model=list[ReleaseManifest],
    tags=["releases"],
)
async def list_releases(
    agent_id: str,
    identity: ManagementIdentity,
    container: Container,
    environment: str | None = Query(default=None),
) -> list[ReleaseManifest]:
    """查询环境内的发布记录；environment 过滤不会绕过租户授权。"""
    return await service(container).list_releases(identity, agent_id, environment)


@router.get("/v1/releases/{release_id}", response_model=ReleaseManifest, tags=["releases"])
async def get_release(
    release_id: str,
    identity: ManagementIdentity,
    container: Container,
) -> ReleaseManifest:
    """读取发布状态及不可变快照引用，供审批和故障诊断使用。"""
    return await service(container).get_release(identity, release_id)


@router.post(
    "/v1/releases/{release_id}/promote",
    response_model=ReleaseManifest,
    tags=["releases"],
)
async def promote_release(
    release_id: str,
    request: ReleasePromote,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> ReleaseManifest:
    """推进已审批发布；服务层校验状态转换、质量门禁与并发版本约束。"""
    return await service(container).promote_release(identity, release_id, request, trace_id)


@router.post(
    "/v1/releases/{release_id}/governance-action",
    response_model=ReleaseManifest,
    tags=["releases"],
)
async def apply_governance_release_action(
    release_id: str,
    request: GovernanceReleaseAction,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> ReleaseManifest:
    """由 Control Plane 拉取 GateDecision 后推进、暂停或回滚，Governance 不直接修改流量。"""
    return await service(container).apply_governance_release_action(
        identity, release_id, request, trace_id
    )


@router.post(
    "/v1/releases/{release_id}/pause",
    response_model=ReleaseManifest,
    tags=["releases"],
)
async def pause_release(
    release_id: str,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> ReleaseManifest:
    """暂停生效发布的流量选择，不修改其冻结快照，便于后续审计恢复。"""
    return await service(container).pause_release(identity, release_id, trace_id)


@router.post(
    "/v1/releases/{release_id}/rollback",
    response_model=ReleaseManifest,
    tags=["releases"],
)
async def rollback_release(
    release_id: str,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> ReleaseManifest:
    """执行受控回滚；服务层选择兼容的历史版本并记录可追溯事件。"""
    return await service(container).rollback_release(identity, release_id, trace_id)


@router.get(
    "/v1/runtime/agents/{agent_id}/resolve",
    response_model=RuntimeResolution,
    tags=["runtime"],
)
async def resolve_runtime(
    agent_id: str,
    identity: RuntimeIdentity,
    container: Container,
    environment: str = Query(default="production"),
    session_id: str = Query(min_length=1, max_length=200),
) -> RuntimeResolution:
    """解析Runtime 身份的 HTTP
    入口：先从已验证身份构造租户上下文，再调用应用服务；路由层不复制授权或状态机逻辑。

    Return the published snapshot selected for one authenticated Runtime run.
    """
    return await service(container).resolve_runtime(identity, agent_id, environment, session_id)


@router.get(
    "/v1/runtime/releases/{release_id}/snapshot",
    response_model=PublishedSnapshot,
    tags=["runtime"],
)
async def get_runtime_snapshot(
    release_id: str,
    identity: RuntimeIdentity,
    container: Container,
) -> PublishedSnapshot:
    """仅向 Runtime 身份返回执行快照，避免管理草稿进入运行路径。"""
    return await service(container).get_release_snapshot(identity, release_id)


@router.get("/v1/tenant-policy", response_model=TenantPolicy, tags=["policy"])
async def get_tenant_policy(
    identity: ManagementIdentity,
    container: Container,
) -> TenantPolicy:
    """读取当前租户发布策略，策略变更必须走受鉴权的管理边界。"""
    return await service(container).get_tenant_policy(identity)


@router.put("/v1/tenant-policy", response_model=TenantPolicy, tags=["policy"])
async def update_tenant_policy(
    request: TenantPolicyUpdate,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> TenantPolicy:
    """更新租户策略并写入审计 Outbox；策略在后续发布校验中生效。"""
    return await service(container).update_tenant_policy(identity, request, trace_id)


@router.get("/v1/tenants", response_model=list[Tenant], tags=["tenants"])
async def list_tenants(identity: ManagementIdentity, container: Container) -> list[Tenant]:
    """列出平台租户目录；服务只允许最高管理员跨租户查看。"""
    return await service(container).list_tenants(identity)


@router.get("/v1/tenants/{tenant_id}", response_model=Tenant, tags=["tenants"])
async def get_tenant(tenant_id: str, identity: ManagementIdentity, container: Container) -> Tenant:
    """读取一个租户的元数据与状态，不返回任何用户或业务内容。"""
    return await service(container).get_tenant(identity, tenant_id)


@router.post("/v1/tenants", response_model=Tenant, status_code=201, tags=["tenants"])
async def create_tenant(
    request: TenantCreate,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> Tenant:
    """建立不可变 ID 的租户目录记录、默认策略和 Outbox 审计事实。"""
    return await service(container).create_tenant(identity, request, trace_id)


@router.put("/v1/tenants/{tenant_id}", response_model=Tenant, tags=["tenants"])
async def update_tenant(
    tenant_id: str,
    request: TenantUpdate,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> Tenant:
    """修改展示元数据或软冻结租户；不提供破坏审计链的删除接口。"""
    return await service(container).update_tenant(identity, tenant_id, request, trace_id)


@router.get("/v1/outbox", response_model=OutboxList, tags=["integration"])
async def list_outbox(
    identity: ManagementIdentity,
    container: Container,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1_000),
) -> OutboxList:
    """按单调序号读取事务 Outbox，供 CDC 或受控 Relay 断点续传。"""
    return await service(container).list_outbox(identity, after_sequence, limit)


@router.post("/v1/model-route-releases", tags=["model-route-releases"])
async def start_model_route_release(
    request: dict[str, Any],
    identity: ManagementIdentity,
    container: Container,
) -> dict[str, Any]:
    """启动模型路由灰度发布；编排器只在候选状态有效时创建监控工作流。"""
    release = await container.model_releases.start(identity.tenant_id, request)
    if container.release_orchestrator is not None and release.get("status") in {
        "CANARY_ACTIVE",
        "MONITORING",
    }:
        container.release_orchestrator.start(
            identity.tenant_id,
            str(release["id"]),
            container.settings.model_release_monitor_interval_seconds,
        )
    return release


@router.get("/v1/model-route-releases", tags=["model-route-releases"])
async def list_model_route_releases(
    identity: ManagementIdentity, container: Container
) -> list[dict[str, Any]]:
    """列出租户模型路由发布，避免跨租户查看供应商或模型策略。"""
    return await container.model_releases.list(identity.tenant_id)


@router.get("/v1/model-route-releases/{release_id}", tags=["model-route-releases"])
async def get_model_route_release(
    release_id: str, identity: ManagementIdentity, container: Container
) -> dict[str, Any]:
    """读取一个模型路由发布及其当前监控决策。"""
    return await container.model_releases.get(identity.tenant_id, release_id)


@router.post("/v1/model-route-releases/{release_id}/monitor", tags=["model-route-releases"])
async def monitor_model_route_release(
    release_id: str, identity: ManagementIdentity, container: Container
) -> dict[str, Any]:
    """立即执行一次灰度指标评估；失败策略由模型发布服务集中决定。"""
    return await container.model_releases.monitor(identity.tenant_id, release_id)


@router.post("/v1/model-route-releases/{release_id}/rollback", tags=["model-route-releases"])
async def rollback_model_route_release(
    release_id: str,
    identity: ManagementIdentity,
    container: Container,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """以人工提供原因回滚模型路由，保留原因以支持后续合规复盘。"""
    reason = str((request or {}).get("reason") or "manual rollback")
    return await container.model_releases.rollback(identity.tenant_id, release_id, reason)

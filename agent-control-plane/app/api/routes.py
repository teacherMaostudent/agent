from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

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
    Identity,
    OutboxList,
    PublishedSnapshot,
    ReleaseCreate,
    ReleaseManifest,
    ReleasePromote,
    RuntimeResolution,
    TenantPolicy,
    TenantPolicyUpdate,
    ValidationReport,
)

ManagementIdentity = Annotated[Identity, Depends(management_identity)]
RuntimeIdentity = Annotated[Identity, Depends(runtime_identity)]
Container = Annotated[AppContainer, Depends(get_container)]
TraceId = Annotated[str, Depends(get_trace_id)]

router = APIRouter()


def service(container: AppContainer) -> ControlPlaneService:
    return container.service


@router.get("/health/live", response_model=HealthStatus, tags=["health"])
async def liveness() -> HealthStatus:
    return HealthStatus(status="ok")


@router.get("/health/ready", response_model=HealthStatus, tags=["health"])
async def readiness(container: Container, response: Response) -> HealthStatus:
    if not await container.repository.healthcheck():
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthStatus(status="ok")


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
    return await service(container).create_agent(identity, request, trace_id)


@router.get("/v1/agents", response_model=list[AgentDefinition], tags=["agents"])
async def list_agents(
    identity: ManagementIdentity,
    container: Container,
) -> list[AgentDefinition]:
    return await service(container).list_agents(identity)


@router.get("/v1/agents/{agent_id}", response_model=AgentDefinition, tags=["agents"])
async def get_agent(
    agent_id: str,
    identity: ManagementIdentity,
    container: Container,
) -> AgentDefinition:
    return await service(container).get_agent(identity, agent_id)


@router.put("/v1/agents/{agent_id}/draft", response_model=AgentDefinition, tags=["agents"])
async def update_agent_draft(
    agent_id: str,
    request: AgentDraftUpdate,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> AgentDefinition:
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
    return await service(container).list_releases(identity, agent_id, environment)


@router.get("/v1/releases/{release_id}", response_model=ReleaseManifest, tags=["releases"])
async def get_release(
    release_id: str,
    identity: ManagementIdentity,
    container: Container,
) -> ReleaseManifest:
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
    return await service(container).promote_release(identity, release_id, request, trace_id)


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
    return await service(container).get_release_snapshot(identity, release_id)


@router.get("/v1/tenant-policy", response_model=TenantPolicy, tags=["policy"])
async def get_tenant_policy(
    identity: ManagementIdentity,
    container: Container,
) -> TenantPolicy:
    return await service(container).get_tenant_policy(identity)


@router.put("/v1/tenant-policy", response_model=TenantPolicy, tags=["policy"])
async def update_tenant_policy(
    request: TenantPolicyUpdate,
    identity: ManagementIdentity,
    container: Container,
    trace_id: TraceId,
) -> TenantPolicy:
    return await service(container).update_tenant_policy(identity, request, trace_id)


@router.get("/v1/outbox", response_model=OutboxList, tags=["integration"])
async def list_outbox(
    identity: ManagementIdentity,
    container: Container,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1_000),
) -> OutboxList:
    return await service(container).list_outbox(identity, after_sequence, limit)


@router.post("/v1/model-route-releases", tags=["model-route-releases"])
async def start_model_route_release(
    request: dict[str, Any],
    identity: ManagementIdentity,
    container: Container,
) -> dict[str, Any]:
    return await container.model_releases.start(identity.tenant_id, request)


@router.get("/v1/model-route-releases", tags=["model-route-releases"])
async def list_model_route_releases(
    identity: ManagementIdentity, container: Container
) -> list[dict[str, Any]]:
    return await container.model_releases.list(identity.tenant_id)


@router.get("/v1/model-route-releases/{release_id}", tags=["model-route-releases"])
async def get_model_route_release(
    release_id: str, identity: ManagementIdentity, container: Container
) -> dict[str, Any]:
    return await container.model_releases.get(identity.tenant_id, release_id)


@router.post(
    "/v1/model-route-releases/{release_id}/monitor", tags=["model-route-releases"]
)
async def monitor_model_route_release(
    release_id: str, identity: ManagementIdentity, container: Container
) -> dict[str, Any]:
    return await container.model_releases.monitor(identity.tenant_id, release_id)


@router.post(
    "/v1/model-route-releases/{release_id}/rollback", tags=["model-route-releases"]
)
async def rollback_model_route_release(
    release_id: str,
    identity: ManagementIdentity,
    container: Container,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reason = str((request or {}).get("reason") or "manual rollback")
    return await container.model_releases.rollback(identity.tenant_id, release_id, reason)

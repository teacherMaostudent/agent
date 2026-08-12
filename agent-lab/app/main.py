"""独立 Agent Lab API：只管理离线实验计划与提交，不在 Web 进程执行长回放。"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from platform_infra.identity import OidcIdentityMiddleware
from platform_infra.telemetry import configure_telemetry

from app.container import AgentLabContainer
from app.main_settings import Settings
from app.models import ExperimentJob, ExperimentPlan, ExperimentRecord


def create_app(settings: Settings | None = None) -> FastAPI:
    """装配独立容器、安全中间件和生命周期；API 与 Worker 从不共享进程内状态。"""
    resolved = settings or Settings()
    container = AgentLabContainer(resolved)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """将容器交给应用生命周期管理，退出时停止 Temporal 提交循环再释放仓储。"""
        application.state.container = container
        try:
            yield
        finally:
            container.close()

    application = FastAPI(title="Agent Platform Agent Lab", version="1.0.0", lifespan=lifespan)
    application.state.container = container
    application.add_middleware(
        OidcIdentityMiddleware,
        enabled=resolved.oidc_enabled,
        issuer=resolved.oidc_issuer,
        audience=resolved.oidc_audience,
        jwks_url=resolved.oidc_jwks_url,
        public_paths=("/health/live", "/health/ready"),
        trusted_workload_prefixes=(),
    )
    configure_telemetry(
        application,
        enabled=getattr(resolved, "otel_enabled", False),
        service_name="agent-lab",
        environment=resolved.environment,
        endpoint=getattr(resolved, "otel_endpoint", ""),
    )
    return application


app = create_app()


def container() -> AgentLabContainer:
    """返回已装配的服务容器，路由不自行创建数据库连接或 Worker。"""
    return app.state.container


def service():
    """返回应用服务，保持路由只负责 HTTP 映射与经验证的租户边界。"""
    return container().service


def require_internal_key(x_agent_lab_key: str | None = Header(default=None)) -> None:
    """保护发布证据接口；OIDC/mTLS 已验证调用工作负载，密钥仅作迁移期纵深防御。"""
    expected = container().settings.service_api_key
    if expected and not secrets.compare_digest(x_agent_lab_key or "", expected):
        raise HTTPException(401, "invalid Agent Lab service credential")


def verified_tenant(request: Request, tenant_id: str) -> str:
    """在 OIDC 启用时将查询租户与验签后 Header 对齐，拒绝通过参数越过身份隔离。"""
    if container().settings.oidc_enabled:
        identity_tenant = request.headers.get("X-Tenant-Id", "")
        if not identity_tenant or identity_tenant != tenant_id:
            raise HTTPException(403, "tenant does not match authenticated identity")
    return tenant_id


@app.get("/health/live")
def liveness() -> dict[str, str]:
    """报告 API 进程存活，不把下游服务短暂故障误报为本进程不可用。"""
    return {"status": "ok"}


@app.get("/health/ready")
def readiness() -> dict[str, str]:
    """报告依赖容器已装配；生产启动配置错误会在进程接流量前失败。"""
    return {"status": "ok", "persistence": container().settings.database_backend}


@app.post("/v1/experiments", response_model=ExperimentRecord, status_code=201)
def create_experiment(plan: ExperimentPlan, request: Request) -> ExperimentRecord:
    """登记不可变回放计划；OIDC 模式下计划租户必须属于经过验证的调用方。"""
    verified_tenant(request, plan.tenant_id)
    try:
        return service().create(plan)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/v1/experiments/{experiment_id}", response_model=ExperimentRecord)
def get_experiment(experiment_id: str, tenant_id: str, request: Request) -> ExperimentRecord:
    """读取本租户实验及冻结快照、回放结果与 Governance 结论。"""
    try:
        return service().get(verified_tenant(request, tenant_id), experiment_id)
    except KeyError as exc:
        raise HTTPException(404, "experiment not found") from exc


@app.post("/v1/experiments/{experiment_id}/prepare", response_model=ExperimentRecord)
def prepare_experiment(experiment_id: str, tenant_id: str, request: Request) -> ExperimentRecord:
    """预解析并冻结发布快照；若灰度尚未稳定则拒绝而非产生混杂实验。"""
    try:
        return service().prepare(verified_tenant(request, tenant_id), experiment_id)
    except KeyError as exc:
        raise HTTPException(404, "experiment not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/v1/experiments/{experiment_id}/run", response_model=ExperimentJob, status_code=202)
def submit_experiment(experiment_id: str, tenant_id: str, request: Request) -> ExperimentJob:
    """提交持久化回放任务；Temporal Worker 才能执行 Runtime 与 Governance 长操作。"""
    resolved_tenant = verified_tenant(request, tenant_id)
    try:
        job = service().submit(
            resolved_tenant,
            experiment_id,
            max_attempts=container().settings.job_max_attempts,
        )
        container().queue.submit(job.job_id)
        return container().repository.get_job(resolved_tenant, job.job_id) or job
    except KeyError as exc:
        raise HTTPException(404, "experiment not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/v1/jobs/{job_id}", response_model=ExperimentJob)
def get_job(job_id: str, tenant_id: str, request: Request) -> ExperimentJob:
    """查询回放调度状态，包括重试次数、租约和是否已进入可审计 DLQ。"""
    job = container().repository.get_job(verified_tenant(request, tenant_id), job_id)
    if job is None:
        raise HTTPException(404, "experiment job not found")
    return job


@app.get("/v1/experiments/{experiment_id}/comparison")
def compare_experiment(experiment_id: str, tenant_id: str, request: Request) -> dict:
    """返回与已声明基线的可解释差异；质量门禁结论仍由 Governance 独占。"""
    try:
        return service().comparison(verified_tenant(request, tenant_id), experiment_id)
    except KeyError as exc:
        raise HTTPException(404, "experiment not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/internal/v1/experiments/{experiment_id}/release-evidence")
def release_evidence(
    experiment_id: str,
    tenant_id: str,
    request: Request,
    _: None = Depends(require_internal_key),
) -> dict:
    """仅为 Control Plane 提供通过门禁的不可变实验事实，拒绝普通业务读取或伪造。"""
    try:
        return service().release_evidence(verified_tenant(request, tenant_id), experiment_id)
    except KeyError as exc:
        raise HTTPException(404, "experiment not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

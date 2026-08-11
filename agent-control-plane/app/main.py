from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from platform_infra.identity import OidcIdentityMiddleware
from platform_infra.telemetry import configure_telemetry

from app.api.routes import router
from app.application.exceptions import (
    ConflictError,
    ControlPlaneError,
    DraftValidationError,
    InvalidStateError,
    NotFoundError,
    PolicyViolationError,
)
from app.container import AppContainer
from app.core.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """组装 HTTP 服务及其安全中间件；身份认证先于所有发布管理路由执行。"""
    resolved_settings = settings or Settings()
    container = AppContainer(resolved_settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """在开始接流量前初始化存储，并保证关闭时停止后台协调器。"""
        await container.start()
        application.state.container = container
        try:
            yield
        finally:
            await container.stop()

    application = FastAPI(
        title="Agent Control Plane",
        version="0.1.0",
        description=(
            "Agent configuration, immutable version snapshots, tenant policy, "
            "canary release, rollback, and runtime resolution."
        ),
        lifespan=lifespan,
    )
    if resolved_settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=resolved_settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    application.add_middleware(
        OidcIdentityMiddleware,
        enabled=resolved_settings.oidc_enabled,
        issuer=resolved_settings.oidc_issuer,
        audience=resolved_settings.oidc_audience,
        jwks_url=resolved_settings.oidc_jwks_url,
        public_paths=("/health/live", "/health/ready"),
        trusted_workload_prefixes=(),
    )

    @application.middleware("http")
    async def trace_context(request: Request, call_next):
        """沿用可信 Trace ID 或生成新 ID，并将其回写到响应便于跨服务关联。"""
        request.state.trace_id = request.headers.get("X-Trace-Id") or f"trace_{uuid4().hex}"
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    @application.exception_handler(ControlPlaneError)
    async def control_plane_exception_handler(
        request: Request,
        error: ControlPlaneError,
    ) -> JSONResponse:
        """将受控领域错误映射为稳定 HTTP 语义，避免泄漏底层存储异常。"""
        del request
        status_code = 400
        if isinstance(error, NotFoundError):
            status_code = 404
        elif isinstance(error, ConflictError):
            status_code = 409
        elif isinstance(error, (DraftValidationError, PolicyViolationError)):
            status_code = 422
        elif isinstance(error, InvalidStateError):
            status_code = 409
        return JSONResponse(
            status_code=status_code,
            content={
                "code": error.code,
                "message": error.message,
                "details": error.details,
            },
        )

    application.include_router(router)
    configure_telemetry(
        application,
        enabled=resolved_settings.otel_enabled,
        service_name=resolved_settings.service_name,
        environment=resolved_settings.environment,
        endpoint=resolved_settings.otel_endpoint,
    )
    return application


app = create_app()

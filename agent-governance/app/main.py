from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from platform_infra.identity import OidcIdentityMiddleware
from platform_infra.telemetry import configure_telemetry

from app.api.routes import router
from app.application.exceptions import GovernanceError, InvalidStateError, NotFoundError
from app.container import AppContainer
from app.core.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    container = AppContainer(resolved_settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        await container.start()
        application.state.container = container
        yield

    application = FastAPI(
        title="Agent Governance",
        version="0.1.0",
        description=(
            "Asynchronous audit, evaluation, and compliance reporting for enterprise agents."
        ),
        lifespan=lifespan,
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

    @application.exception_handler(GovernanceError)
    async def governance_error_handler(_: Request, error: GovernanceError) -> JSONResponse:
        status_code = (
            404
            if isinstance(error, NotFoundError)
            else 409
            if isinstance(error, InvalidStateError)
            else 400
        )
        return JSONResponse(
            status_code=status_code,
            content={"code": error.code, "message": error.message, "details": error.details},
        )

    application.include_router(router)
    configure_telemetry(
        application,
        enabled=resolved_settings.otel_enabled,
        service_name="agent-governance",
        environment=resolved_settings.environment,
        endpoint=resolved_settings.otel_endpoint,
    )
    return application


app = create_app()

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from platform_infra.identity import OidcIdentityMiddleware
from platform_infra.telemetry import configure_telemetry

from app.api.routes import router
from app.container import Container
from app.domain.errors import GatewayError


def create_app(container: Container | None = None) -> FastAPI:
    service_container = container or Container()
    settings = service_container.settings

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        service_container.close()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.container = service_container
    app.add_middleware(
        OidcIdentityMiddleware,
        enabled=settings.oidc_enabled,
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        jwks_url=settings.oidc_jwks_url,
        public_paths=(
            f"{settings.api_prefix}/health",
            f"{settings.api_prefix}/health/ready",
        ),
        trusted_workload_prefixes=(),
    )

    def require_admin(request: Request) -> None:
        supplied = request.headers.get("X-Tool-Gateway-Admin-Key", "")
        if not secrets.compare_digest(supplied, settings.admin_api_key):
            from app.domain.errors import ToolPermissionError

            raise ToolPermissionError("admin credential is required")

    app.state.require_admin = require_admin

    @app.middleware("http")
    async def service_auth(request: Request, call_next):
        health_path = request.url.path.endswith(("/health", "/health/ready"))
        content_length = request.headers.get("content-length")
        if (
            content_length
            and content_length.isdigit()
            and int(content_length) > settings.max_request_bytes
        ):
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "request_too_large",
                        "message": "request body exceeds the configured size limit",
                    }
                },
            )
        if settings.require_service_auth and not health_path:
            supplied = request.headers.get("X-Tool-Gateway-Key", "")
            if not secrets.compare_digest(supplied, settings.service_api_key):
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "code": "invalid_service_credential",
                            "message": "invalid service credential",
                        }
                    },
                )
        response = await call_next(request)
        override = request.scope.get("tool_gateway_status_code")
        if override:
            response.status_code = override
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "details": exc.details,
                }
            },
        )

    @app.get(f"{settings.api_prefix}/health", tags=["health"])
    def health() -> dict:
        return {"status": "UP", "service": settings.app_name}

    @app.get(f"{settings.api_prefix}/health/ready", tags=["health"])
    def ready() -> dict:
        return {"status": "UP", "service": settings.app_name, **service_container.ready()}

    app.include_router(router, prefix=settings.api_prefix)
    configure_telemetry(
        app,
        enabled=settings.otel_enabled,
        service_name=settings.app_name,
        environment=settings.environment,
        endpoint=settings.otel_endpoint,
    )
    return app


app = create_app()

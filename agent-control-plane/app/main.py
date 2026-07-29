from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
    resolved_settings = settings or Settings()
    container = AppContainer(resolved_settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
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

    @application.middleware("http")
    async def trace_context(request: Request, call_next):
        request.state.trace_id = request.headers.get("X-Trace-Id") or f"trace_{uuid4().hex}"
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    @application.exception_handler(ControlPlaneError)
    async def control_plane_exception_handler(
        request: Request,
        error: ControlPlaneError,
    ) -> JSONResponse:
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
    return application


app = create_app()

import secrets
from contextlib import asynccontextmanager
from collections.abc import Callable, Iterable

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.logging import configure_logging
from app.observability import configure_tracing, instrument_fastapi


def create_service_app(
    service_name: str,
    container,
    routers: Iterable[APIRouter],
    readiness: Callable[[], dict] | None = None,
) -> FastAPI:
    """Create one deployable process with consistent auth, CORS, health and tracing."""
    configure_logging()
    settings = container.settings
    tracing_settings = settings.model_copy(update={"otel_service_name": service_name})
    configure_tracing(tracing_settings)
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        close = getattr(container, "close", None)
        if close is not None:
            close()

    app = FastAPI(title=service_name, version="0.2.0", lifespan=lifespan)
    app.state.container = container

    @app.middleware("http")
    async def service_auth(request: Request, call_next):
        if settings.require_service_auth and not request.url.path.endswith(("/health", "/health/ready")):
            supplied = request.headers.get("X-Rag-Agent-Key", "")
            expected = settings.internal_service_api_key or settings.service_api_key
            if not secrets.compare_digest(supplied, expected):
                return JSONResponse(status_code=401, content={"detail": "invalid service credential"})
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get(f"{settings.api_prefix}/health", tags=["health"])
    def health() -> dict:
        return {"status": "UP", "service": service_name}

    @app.get(f"{settings.api_prefix}/health/ready", tags=["health"])
    def ready() -> dict:
        try:
            detail = readiness() if readiness is not None else {}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"dependency unavailable: {type(exc).__name__}") from exc
        return {"status": "UP", "service": service_name, **detail}

    for router in routers:
        app.include_router(router, prefix=settings.api_prefix)
    instrument_fastapi(app, tracing_settings)
    return app

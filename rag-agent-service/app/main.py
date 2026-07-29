import secrets

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    agent_api,
    cross_document_api,
    document_api,
    evaluation_api,
    export_api,
    generation_api,
    health_api,
    knowledge_api,
    review_api,
)
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.observability import configure_tracing, instrument_fastapi


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    configure_tracing(settings)
    app = FastAPI(title=settings.app_name, version="0.1.0")
    instrument_fastapi(app, settings)

    @app.middleware("http")
    async def service_auth(request, call_next):
        if settings.require_service_auth and not request.url.path.endswith(("/health", "/health/ready")):
            supplied = request.headers.get("X-Rag-Agent-Key", "")
            if not secrets.compare_digest(supplied, settings.service_api_key):
                return JSONResponse(status_code=401, content={"detail": "invalid service credential"})
        return await call_next(request)
    # 独立业务前端跑在不同端口(如 5173)，跨域访问需放行。
    # 开发默认全放行；部署时用 RAG_CORS_ORIGINS 收窄到具体域名。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        # 通配来源(*) 与 credentials 不能并存；本服务无 cookie 鉴权，关掉即可。
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health_api.router, prefix=settings.api_prefix)
    app.include_router(document_api.router, prefix=settings.api_prefix)
    app.include_router(knowledge_api.router, prefix=settings.api_prefix)
    app.include_router(review_api.router, prefix=settings.api_prefix)
    app.include_router(cross_document_api.router, prefix=settings.api_prefix)
    app.include_router(generation_api.router, prefix=settings.api_prefix)
    app.include_router(export_api.router, prefix=settings.api_prefix)
    app.include_router(agent_api.router, prefix=settings.api_prefix)
    app.include_router(evaluation_api.router, prefix=settings.api_prefix)
    return app


application = create_app()
app = application


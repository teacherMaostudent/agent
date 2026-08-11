import secrets
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from platform_infra.identity import OidcIdentityMiddleware
from platform_infra.opa import OpaAuthorizationMiddleware

from app.core.logging import configure_logging
from app.observability import configure_tracing, instrument_fastapi


def create_service_app(
    service_name: str,
    container,
    routers: Iterable[APIRouter],
    readiness: Callable[[], dict] | None = None,
) -> FastAPI:
    """创建一致的服务边界：认证、授权、CORS、健康检查、追踪及退出清理。

    业务 Router 只暴露自身能力；OIDC/OPA 与服务间密钥在统一中间件中执行，
    防止各 API 入口遗漏身份或策略校验。
    """
    configure_logging()
    settings = container.settings
    tracing_settings = settings.model_copy(update={"otel_service_name": service_name})
    configure_tracing(tracing_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        """在 ASGI 生命周期结束时关闭容器持有的存储和网络资源。"""
        yield
        close = getattr(container, "close", None)
        if close is not None:
            close()

    app = FastAPI(title=service_name, version="0.2.0", lifespan=lifespan)
    app.state.container = container

    @app.middleware("http")
    async def service_auth(request: Request, call_next):
        """验证服务间共享凭据；健康端点例外以支持编排器探针。

        该兼容层与 mTLS/OIDC 并存，生产身份逐步迁移到工作负载令牌后仍保留
        常量时间比较，避免因错误配置放开内部 API。
        """
        if settings.require_service_auth and not request.url.path.endswith(
            ("/health", "/health/ready")
        ):
            supplied = request.headers.get("X-Rag-Agent-Key", "")
            expected = settings.internal_service_api_key or settings.service_api_key
            if not secrets.compare_digest(supplied, expected):
                return JSONResponse(
                    status_code=401, content={"detail": "invalid service credential"}
                )
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    public_paths = (
        f"{settings.api_prefix}/health",
        f"{settings.api_prefix}/health/ready",
    )
    app.add_middleware(
        OpaAuthorizationMiddleware,
        enabled=settings.opa_enabled,
        base_url=settings.opa_base_url,
        decision_path=settings.opa_decision_path,
        public_paths=public_paths,
        trusted_workload_prefixes=(),
    )
    app.add_middleware(
        OidcIdentityMiddleware,
        enabled=settings.oidc_enabled,
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        jwks_url=settings.oidc_jwks_url,
        permissions_claim=getattr(settings, "oidc_permissions_claim", "permissions"),
        public_paths=public_paths,
    )

    @app.get(f"{settings.api_prefix}/health", tags=["health"])
    def health() -> dict:
        """报告进程存活，不访问外部依赖，因此适合 liveness probe。"""
        return {"status": "UP", "service": service_name}

    @app.get(f"{settings.api_prefix}/health/ready", tags=["health"])
    def ready() -> dict:
        """检查可选依赖；异常转换为 503，供流量入口停止向未就绪副本路由。"""
        try:
            detail = readiness() if readiness is not None else {}
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail=f"dependency unavailable: {type(exc).__name__}"
            ) from exc
        return {"status": "UP", "service": service_name, **detail}

    for router in routers:
        app.include_router(router, prefix=settings.api_prefix)
    instrument_fastapi(app, tracing_settings)
    return app

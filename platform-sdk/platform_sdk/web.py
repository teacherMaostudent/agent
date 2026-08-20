"""Framework-neutral service factory shared by independently deployed services."""

import secrets
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from platform_infra.identity import OidcIdentityMiddleware
from platform_infra.opa import OpaAuthorizationMiddleware


def create_service_app(
    service_name: str,
    container,
    routers: Iterable[APIRouter],
    readiness: Callable[[], dict] | None = None,
) -> FastAPI:
    """为独立部署服务安装统一生命周期、身份、授权与健康边界。

    过渡期服务密钥只用于内部调用兼容；OIDC 与 OPA 中间件仍负责已验证主体和策略
    决策。业务路由在最后挂载，确保其无法绕过先注册的安全中间件。
    """
    settings = container.settings

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        """在进程退出时释放容器拥有的连接池、队列和后台资源。"""
        yield
        close = getattr(container, "close", None)
        if close is not None:
            close()

    app = FastAPI(title=service_name, version="1.0.0", lifespan=lifespan)
    app.state.container = container

    @app.middleware("http")
    async def service_auth(request: Request, call_next):
        """校验内部服务密钥的兼容层；健康端点保持可探测且不暴露业务数据。"""
        if getattr(
            settings, "require_service_auth", False
        ) and not request.url.path.endswith(("/health", "/health/ready")):
            expected = getattr(settings, "internal_service_api_key", "") or getattr(
                settings, "service_api_key", ""
            )
            if not secrets.compare_digest(
                request.headers.get("X-Rag-Agent-Key", ""), expected
            ):
                return JSONResponse(
                    status_code=401, content={"detail": "invalid service credential"}
                )
        return await call_next(request)

    prefix = getattr(settings, "api_prefix", "/api/v1")
    public_paths = (f"{prefix}/health", f"{prefix}/health/ready")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=getattr(settings, "cors_origins", []),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        OpaAuthorizationMiddleware,
        enabled=getattr(settings, "opa_enabled", False),
        base_url=getattr(settings, "opa_base_url", ""),
        decision_path=getattr(settings, "opa_decision_path", "agent_platform/allow"),
        public_paths=public_paths,
        trusted_workload_prefixes=(),
    )
    app.add_middleware(
        OidcIdentityMiddleware,
        enabled=getattr(settings, "oidc_enabled", False),
        issuer=getattr(settings, "oidc_issuer", ""),
        audience=getattr(settings, "oidc_audience", "agent-platform"),
        jwks_url=getattr(settings, "oidc_jwks_url", ""),
        public_paths=public_paths,
    )

    @app.get(f"{prefix}/health")
    def health() -> dict:
        """返回进程存活信号，不触发依赖检查。"""
        return {"status": "UP", "service": service_name}

    @app.get(f"{prefix}/health/ready")
    def ready() -> dict:
        """执行注入的依赖就绪检查，失败时统一返回 503 且不泄露内部异常详情。"""
        try:
            return {
                "status": "UP",
                "service": service_name,
                **(readiness() if readiness else {}),
            }
        except Exception as exc:
            raise HTTPException(status_code=503, detail=type(exc).__name__) from exc

    for router in routers:
        app.include_router(router, prefix=prefix)
    return app

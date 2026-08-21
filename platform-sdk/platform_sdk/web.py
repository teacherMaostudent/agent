"""Framework-neutral service factory shared by independently deployed services."""

import secrets
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from platform_infra.identity import OidcIdentityMiddleware
from platform_infra.opa import OpaAuthorizationMiddleware


def _requires_service_key(settings, path: str) -> bool:
    """判断请求是否属于内部服务认证范围，并严格阻止相似前缀碰撞。"""
    if not getattr(settings, "require_service_auth", False):
        return False
    if path.endswith(("/health", "/health/ready")):
        return False
    normalized = path.rstrip("/") or "/"
    exempt_paths = {
        item.rstrip("/") or "/"
        for item in getattr(settings, "service_auth_exempt_paths", [])
        if item and item.startswith("/")
    }
    if normalized in exempt_paths:
        return False
    exempt_prefixes = tuple(
        prefix.rstrip("/")
        for prefix in getattr(settings, "service_auth_exempt_prefixes", [])
        if prefix and prefix.startswith("/")
    )
    return not any(
        normalized == prefix or normalized.startswith(f"{prefix}/")
        for prefix in exempt_prefixes
    )


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
        if _requires_service_key(settings, request.url.path):
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
    # 精确公开路径只承载服务说明，不会连带豁免其子路由；业务 API 继续执行身份、
    # OPA 与内部工作负载认证。
    public_paths = (
        f"{prefix}/health",
        f"{prefix}/health/ready",
        *tuple(getattr(settings, "service_auth_exempt_paths", [])),
    )
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

    @app.get(prefix, response_class=HTMLResponse, include_in_schema=False)
    def api_landing() -> str:
        """向直接使用浏览器访问 API 根地址的用户说明用途，不暴露受保护资源。"""
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{service_name}</title><style>
body{{font-family:system-ui,sans-serif;max-width:720px;margin:64px auto;padding:0 24px;color:#1f2937}}
.card{{border:1px solid #d1d5db;border-radius:14px;padding:28px;box-shadow:0 4px 18px #0000000d}}
code{{background:#f3f4f6;padding:3px 7px;border-radius:6px}}a{{color:#2563eb}}
</style></head><body><div class="card"><h1>{service_name} 已运行</h1>
<p>这是 Agent Platform 的 API 服务，不是业务操作网页。</p>
<p>桌面端应连接当前地址；API 根路径为 <code>{prefix}</code>。</p>
<p><a href="{prefix}/health/ready">查看服务健康状态</a></p>
<p>受保护的业务接口必须由桌面端或已认证的服务调用，浏览器直接访问会返回凭据错误。</p>
</div></body></html>"""

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

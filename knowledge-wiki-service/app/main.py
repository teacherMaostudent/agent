"""Knowledge Wiki HTTP API; only human review can publish reusable knowledge."""

import secrets
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from platform_infra.identity import OidcIdentityMiddleware
from platform_infra.schema_registry import SchemaRegistry

from app.config import Settings
from app.models import CompileRequest, ReviewRequest, ReviewResult, WikiCandidate, WikiPage
from app.repository import WikiRepository
from app.service import KnowledgeWikiService

router = APIRouter()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Assemble one API process with an isolated repository and verified identity boundary."""
    resolved = settings or Settings()
    repository = WikiRepository(resolved.database_url)
    service = KnowledgeWikiService(
        repository, schema_registry=SchemaRegistry(resolved.contracts_schema_dir)
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """Expose initialized dependencies and close the SQL pool exactly once at shutdown."""
        application.state.repository = repository
        application.state.service = service
        application.state.settings = resolved
        try:
            yield
        finally:
            repository.close()

    app = FastAPI(title="Agent Platform Knowledge Wiki", version="1.0.0", lifespan=lifespan)
    app.state.repository = repository
    app.state.service = service
    app.state.settings = resolved
    app.add_middleware(
        OidcIdentityMiddleware, enabled=resolved.oidc_enabled, issuer=resolved.oidc_issuer,
        audience=resolved.oidc_audience, jwks_url=resolved.oidc_jwks_url,
        public_paths=("/health/live", "/health/ready"), trusted_workload_prefixes=(),
    )
    app.include_router(router)
    return app


def identity(
    request: Request, tenant: str, user: str, key: str | None
) -> tuple[str, str, set[str]]:
    """Resolve the middleware-issued tenant/user and verify the internal service credential."""
    settings = request.app.state.settings
    if settings.service_api_key and not secrets.compare_digest(key or "", settings.service_api_key):
        raise HTTPException(401, "invalid Knowledge Wiki service credential")
    roles = {item.strip() for item in request.headers.get("X-Roles", "").split(",") if item.strip()}
    if not tenant or not user:
        raise HTTPException(401, "tenant and user identity are required")
    return tenant, user, roles


@router.get("/health/live")
def live() -> dict[str, str]:
    """Report process liveness without touching storage or disclosing configuration."""
    return {"status": "ok"}


@router.get("/health/ready")
def ready(request: Request) -> dict[str, str]:
    """Probe the authoritative database through a tenant-neutral empty read."""
    request.app.state.repository.pages("__health__")
    return {"status": "ok"}


@router.post("/v1/wiki/candidates", response_model=WikiCandidate, status_code=201)
def compile_candidate(
    payload: CompileRequest, request: Request,
    x_tenant_id: str = Header(alias="X-Tenant-Id"),
    x_user_id: str = Header(alias="X-User-Id"),
    x_service_key: str | None = Header(default=None, alias="X-Knowledge-Wiki-Key"),
) -> WikiCandidate:
    """Accept a provenance-bearing candidate but never publish it from the compile endpoint."""
    tenant, user, _ = identity(request, x_tenant_id, x_user_id, x_service_key)
    return request.app.state.service.compile(tenant, user, payload)


@router.get("/v1/wiki/candidates/{candidate_id}", response_model=WikiCandidate)
def get_candidate(
    candidate_id: str, request: Request,
    x_tenant_id: str = Header(alias="X-Tenant-Id"),
    x_user_id: str = Header(alias="X-User-Id"),
    x_service_key: str | None = Header(default=None, alias="X-Knowledge-Wiki-Key"),
) -> WikiCandidate:
    """Return one same-tenant candidate; invisible and missing objects share a 404 boundary."""
    tenant, _, _ = identity(request, x_tenant_id, x_user_id, x_service_key)
    item = request.app.state.repository.get_candidate(tenant, candidate_id)
    if item is None:
        raise HTTPException(404, "candidate not found")
    return item


@router.get("/v1/wiki/candidates", response_model=list[WikiCandidate])
def list_candidates(
    request: Request,
    status: str = "pending_review",
    limit: int = 50,
    x_tenant_id: str = Header(alias="X-Tenant-Id"),
    x_user_id: str = Header(alias="X-User-Id"),
    x_service_key: str | None = Header(default=None, alias="X-Knowledge-Wiki-Key"),
) -> list[WikiCandidate]:
    """Return the bounded expert review queue; ordinary users cannot enumerate candidates."""
    tenant, _, roles = identity(request, x_tenant_id, x_user_id, x_service_key)
    if "knowledge-reviewer" not in roles:
        raise HTTPException(403, "knowledge-reviewer role is required")
    allowed = {"", "pending_review", "approved", "rejected"}
    if status not in allowed:
        raise HTTPException(422, "candidate status is invalid")
    return request.app.state.repository.candidates(tenant, status=status, limit=limit)


@router.post("/v1/wiki/candidates/{candidate_id}/review", response_model=ReviewResult)
def review_candidate(
    candidate_id: str, payload: ReviewRequest, request: Request,
    x_tenant_id: str = Header(alias="X-Tenant-Id"),
    x_user_id: str = Header(alias="X-User-Id"),
    x_service_key: str | None = Header(default=None, alias="X-Knowledge-Wiki-Key"),
) -> ReviewResult:
    """Consume a reviewer decision once and map state conflicts to a stable HTTP response."""
    tenant, user, roles = identity(request, x_tenant_id, x_user_id, x_service_key)
    if "knowledge-reviewer" not in roles:
        raise HTTPException(403, "knowledge-reviewer role is required")
    try:
        return request.app.state.service.review(tenant, user, candidate_id, payload)
    except KeyError as exc:
        raise HTTPException(404, "candidate not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/v1/wiki/pages", response_model=list[WikiPage])
def list_pages(
    request: Request, x_tenant_id: str = Header(alias="X-Tenant-Id"),
    x_user_id: str = Header(alias="X-User-Id"),
    x_service_key: str | None = Header(default=None, alias="X-Knowledge-Wiki-Key"),
) -> list[WikiPage]:
    """List tenant page versions with current expiry and supersede status projected."""
    tenant, _, _ = identity(request, x_tenant_id, x_user_id, x_service_key)
    return request.app.state.service.list_pages(tenant)


app = create_app()

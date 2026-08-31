"""Same-origin BFF: browser projections only, never an Agent execution engine."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from platform_infra.identity import OidcIdentityMiddleware
from platform_infra.mtls import mtls_httpx_options
from starlette.middleware.base import BaseHTTPMiddleware

from agent_web_bff.browser_oidc import (
    PUBLIC_OIDC_PATHS,
    BrowserOidcSessionMiddleware,
    BrowserSessionStore,
    build_auth_router,
)
from agent_web_bff.config import WebBffSettings

settings = WebBffSettings()
app = FastAPI(title="Agent Web BFF", version="0.1.0")
browser_sessions = BrowserSessionStore(settings) if settings.oidc_enabled else None


def _mtls_options() -> dict[str, Any]:
    """Build the BFF's dedicated client-certificate options for every platform hop."""
    return mtls_httpx_options(
        enabled=settings.mtls_enabled,
        ca_file=settings.mtls_ca_file,
        cert_file=settings.mtls_cert_file,
        key_file=settings.mtls_key_file,
    )


class BrowserBoundaryMiddleware(BaseHTTPMiddleware):
    """Apply browser-only security headers and same-origin CSRF checks before Web mutations."""

    async def dispatch(self, request: Request, call_next):
        unsafe = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        origin = request.headers.get("Origin", "")
        enforce_csrf = settings.csrf_enforced or settings.environment.lower() in {"production", "prod"}
        if unsafe and enforce_csrf and origin != settings.public_origin.rstrip("/"):
            return Response("forbidden cross-origin request", status_code=403)
        response = await call_next(request)
        # Two legacy dialog-close attributes share one exact handler body. CSP Level 3 hashes only
        # that body; arbitrary inline script and every external script origin remain forbidden.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; "
            "script-src-attr 'unsafe-hashes' 'sha256-cwM7l1c7O3MZ3hzRmpH0dzMrxdDWZ+dOZr1a+/S2kCo='; "
            "connect-src 'self'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors 'none'; object-src 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        # 当前前端资源使用稳定文件名（app.js/styles.css），浏览器若采用启发式缓存，
        # 即使 BFF 容器已重建也可能继续执行旧权限 UI。要求每次导航重新验证 ETag，
        # 在保留带宽收益的同时确保发布后的页面代码立即生效。
        if request.url.path == "/" or request.url.path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
        return response


app.add_middleware(BrowserBoundaryMiddleware)
app.add_middleware(
    OidcIdentityMiddleware,
    enabled=settings.oidc_enabled,
    issuer=settings.oidc_issuer,
    audience=settings.oidc_audience,
    jwks_url=settings.oidc_jwks_url,
    # Logout must remain public to the token validator: a stale/invalid BFF session is precisely
    # when the browser needs to revoke its opaque cookie and start a clean Authorization Code flow.
    # favicon 是浏览器的辅助请求，不含业务内容；必须与外层会话中间件同时放行，
    # 否则未登录加载图标会触发授权重定向循环。
    public_paths=PUBLIC_OIDC_PATHS,
)
if browser_sessions is not None:
    # Added after the JWT middleware so Starlette executes this outer layer first. It resolves
    # the HttpOnly cookie and injects the token that the inner middleware then validates normally.
    app.add_middleware(BrowserOidcSessionMiddleware, settings=settings, store=browser_sessions)
    app.include_router(build_auth_router(settings, browser_sessions))
else:

    @app.get("/auth/signout", include_in_schema=False)
    async def local_development_signout() -> RedirectResponse:
        """End the browser's local demonstration context without pretending it is IdP logout.

        Local Compose accepts explicit test headers, so it has no persistent authenticated session
        to revoke.  The redirect marks the page as signed out; the UI then requires an explicit
        local identity-template selection before it restores any simulated privileges.
        """
        response = RedirectResponse("/?local_signed_out=1", status_code=303)
        response.delete_cookie(settings.session_cookie_name, path="/")
        return response

    @app.get("/auth/switch-account", include_in_schema=False)
    async def local_development_switch_account() -> RedirectResponse:
        """Return local Compose users to the neutral shell before selecting another test profile."""
        return RedirectResponse("/?local_signed_out=1", status_code=303)


async def _request_object(request: Request) -> dict[str, Any]:
    """统一拒绝损坏 JSON、数组和 null，防止表单输入触发 AttributeError/500。"""
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="request body must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="request body must be a JSON object")
    return payload


def _form_number(payload: dict[str, Any], name: str, default: int | float = 0,
                 *, integer: bool = False, maximum: float | None = None) -> int | float:
    """金额与比例只接受有限非负数，不能把布尔值、NaN 或小数静默转成整数。"""
    value = payload.get(name, default)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or value < 0 or value > 1e308 or not math.isfinite(value)
            or (integer and value != int(value)) or (maximum is not None and value > maximum)):
        raise HTTPException(status_code=422, detail=f"{name} is invalid")
    return int(value) if integer else float(value)


def _form_strings(payload: dict[str, Any], name: str, maximum: int) -> list[str]:
    """列表必须显式提交为字符串数组；不能将租户白名单字符串拆成字符后发布。"""
    values = payload.get(name, [])
    if (not isinstance(values, list) or len(values) > maximum
            or any(not isinstance(value, str) for value in values)):
        raise HTTPException(status_code=422, detail=f"{name} must be a string array of at most {maximum} items")
    return values


def _identity_headers(request: Request) -> dict[str, str]:
    """Forward only middleware-verified identity and the original Bearer token to Runtime.

    In production, identity headers were reconstructed by ``OidcIdentityMiddleware``. In the
    explicitly local mode they remain a development compatibility mechanism and must never be
    exposed through an internet-facing deployment.
    """
    headers = {
        name: request.headers.get(name, "")
        for name in ("Authorization", "X-Tenant-Id", "X-User-Id", "X-Roles", "X-Permissions")
        if request.headers.get(name)
    }
    if settings.runtime_service_key:
        headers["X-Rag-Agent-Key"] = settings.runtime_service_key
    return headers


def _require_permission(request: Request, permission: str) -> None:
    """Enforce a Web action permission after OIDC middleware reconstructs identity headers.

    Local Header mode exists only for Compose development. In production the middleware has
    removed caller supplied values and rebuilt this same header from signed JWT claims, so the
    check remains server-side rather than a navigation-menu convention.
    """
    permissions = {
        item.strip() for item in request.headers.get("X-Permissions", "").split(",") if item.strip()
    }
    if permission not in permissions:
        raise HTTPException(status_code=403, detail=f"{permission} permission is required")


def _require_high_risk_authentication(request: Request) -> None:
    """Require recent MFA/strong ACR for production mutations that change platform state."""
    if settings.environment.lower() not in {"production", "prod"}:
        return
    claims = request.scope.get("auth.claims", {})
    if not isinstance(claims, dict):
        raise HTTPException(status_code=403, detail="strong authentication is required")
    accepted_acr = {
        item.strip() for item in settings.high_risk_acr_values.split(",") if item.strip()
    }
    acr = str(claims.get("acr", ""))
    amr_value = claims.get("amr", [])
    amr = {str(item) for item in amr_value} if isinstance(amr_value, list) else set()
    auth_time = int(claims.get("auth_time", 0) or 0)
    recent = auth_time > 0 and time() - auth_time <= settings.high_risk_max_auth_age_seconds
    if (acr not in accepted_acr and "mfa" not in amr) or not recent:
        raise HTTPException(
            status_code=403,
            detail="recent multi-factor authentication is required for this operation",
        )


def _governance_headers(request: Request) -> dict[str, str]:
    """向 Governance 转发已验证身份，并只为 BFF 服务身份补足审计角色。

    浏览器权限仍在 BFF 逐接口检查；补足角色只是让 Governance 能验证这个服务调用链，
    不是把审计员能力回传给浏览器或扩大用户的 Runtime 权限。
    """
    headers = _identity_headers(request)
    roles = {item.strip() for item in headers.get("X-Roles", "").split(",") if item.strip()}
    roles.add("governance-auditor")
    headers["X-Roles"] = ",".join(sorted(roles))
    if settings.governance_auditor_key:
        headers["X-Governance-Auditor-Key"] = settings.governance_auditor_key
    return headers


def _control_plane_headers(request: Request) -> dict[str, str]:
    """向 Control Plane 转发验证后身份并附加 BFF 的工作负载管理凭据。

    控制面密钥只留在 BFF 容器；每个浏览器动作还需通过这里的细粒度 permission，
    因而用户不能借由页面请求直接获得或复用该凭据。
    """
    headers = _identity_headers(request)
    roles = {item.strip() for item in headers.get("X-Roles", "").split(",") if item.strip()}
    # The BFF is the authenticated management workload. Browser users still need an independent
    # fine-grained action permission before any route reaches this helper.
    roles.add("agent-admin")
    headers["X-Roles"] = ",".join(sorted(roles))
    if settings.control_plane_admin_key:
        headers["X-Control-Plane-Admin-Key"] = settings.control_plane_admin_key
    return headers


async def _identity_admin(method: str, path: str, **kwargs: Any) -> Any:
    """Call Keycloak's Admin API with the BFF service account, never a browser token.

    The feature is optional outside the local IdP overlay.  Production deployments must bind the
    same interface to a least-privilege IdP/SCIM service account rather than a human admin login.
    """
    required = (
        settings.identity_admin_base_url,
        settings.identity_admin_realm,
        settings.identity_admin_client_id,
        settings.identity_admin_client_secret,
    )
    if not all(required):
        raise HTTPException(status_code=503, detail="identity administration is not configured")
    token_url = f"{settings.identity_admin_base_url.rstrip('/')}/realms/{settings.identity_admin_realm}/protocol/openid-connect/token"
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            token = await client.post(
                token_url,
                data={"grant_type": "client_credentials", "client_id": settings.identity_admin_client_id, "client_secret": settings.identity_admin_client_secret},
            )
            token.raise_for_status()
            response = await client.request(
                method,
                f"{settings.identity_admin_base_url.rstrip('/')}/admin/realms/{settings.identity_admin_realm}{path}",
                headers={"Authorization": f"Bearer {token.json()['access_token']}"},
                **kwargs,
            )
    except (httpx.HTTPError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="identity provider administration is unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail="identity provider rejected the request")
    return {} if response.status_code == 204 or not response.content else response.json()


def _identity_administration_configured() -> bool:
    """Return whether human-user administration has a real IdP service-account path.

    Tenant Catalog management stays available through Control Plane, but human-account changes
    require both verified OIDC and a separate least-privilege IdP service account.  Exposing this
    capability in the session projection keeps the UI from offering a writable-looking directory
    when a developer deliberately selected Header-only compatibility mode.
    """
    return all((
        settings.oidc_enabled,
        settings.identity_admin_base_url,
        settings.identity_admin_realm,
        settings.identity_admin_client_id,
        settings.identity_admin_client_secret,
    ))


async def _runtime(request: Request, method: str, path: str, **kwargs: Any) -> Any:
    """Call Runtime as the sole Workspace source of execution state.

    The BFF may shape browser projections but never supplies a different tenant/user, changes a
    Release, or retries a side effect. Runtime retains Run ownership, cancellation and approval
    semantics.
    """
    try:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds, **_mtls_options()
        ) as client:
            response = await client.request(
                method,
                f"{settings.runtime_base_url.rstrip('/')}{path}",
                headers=_identity_headers(request),
                **kwargs,
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="runtime is unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text[:2_000])
    if response.status_code == 204:
        return {}
    return response.json()


async def _control_plane(request: Request, method: str, path: str, **kwargs: Any) -> Any:
    """调用 Control Plane 管理 API；浏览器永远看不到 BFF 的工作负载管理密钥。"""
    try:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds, **_mtls_options()
        ) as client:
            response = await client.request(
                method,
                f"{settings.control_plane_base_url.rstrip('/')}{path}",
                headers=_control_plane_headers(request),
                **kwargs,
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="control plane is unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text[:2_000])
    return {} if response.status_code == 204 else response.json()


async def _governance(request: Request, method: str, path: str, **kwargs: Any) -> Any:
    """Call Governance with the BFF workload credential and browser's verified tenant.

    Governance remains the owner of export jobs and audit data. The BFF only applies
    action permissions/MFA and shapes the same-origin response; it never handles WORM
    object credentials or proxies exported audit content into the browser.
    """
    try:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds, **_mtls_options()
        ) as client:
            response = await client.request(
                method,
                f"{settings.governance_base_url.rstrip('/')}{path}",
                headers=_governance_headers(request),
                **kwargs,
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="governance is unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text[:2_000])
    return {} if response.status_code == 204 else response.json()


def _event_metadata_projection(metadata: Any) -> dict[str, Any]:
    """Project auditable event facts without returning prompts, responses, or tool payloads."""
    if not isinstance(metadata, dict):
        return {}
    scalar_keys = {
        "origin",
        "previous_state",
        "current_state",
        "trigger",
        "source_event_type",
        "message_source",
        "context_source",
        "history_count",
        "selection_policy",
        "admission_id",
        "plan_id",
        "policy_version",
        "step",
        "step_id",
        "epoch_id",
        "model_route",
        "model_policy_version",
        "model_revision",
        "logical_model",
        "prompt_version",
        "tool_catalog_version",
        "action",
        "outcome",
        "tool",
        "tool_name",
        "tool_version",
        "success",
        "result_count",
        "retrieval_profile",
        "rag_status",
        "degraded",
        "termination_reason",
        "steps",
        "reason",
        "approval_id",
    }
    projected = {
        key: (value[:500] if isinstance(value, str) else value)
        for key, value in metadata.items()
        if key in scalar_keys and isinstance(value, (str, int, float, bool))
    }
    allowed_tools = metadata.get("allowed_tool_scope")
    if isinstance(allowed_tools, list):
        projected["allowed_tool_scope"] = [str(item)[:120] for item in allowed_tools[:20]]
    checks = metadata.get("checks")
    if isinstance(checks, list):
        projected["checks"] = [
            {
                "check": str(item.get("check", ""))[:120],
                "passed": bool(item.get("passed", False)),
                "reason": str(item.get("reason", ""))[:500],
            }
            for item in checks[:30]
            if isinstance(item, dict)
        ]
    budget_report = metadata.get("budget_report")
    if isinstance(budget_report, dict):
        projected["budget_report"] = {
            key: value
            for key, value in budget_report.items()
            if key
            in {
                "requested_tokens",
                "message_budget",
                "evidence_budget",
                "used_message_tokens",
                "used_evidence_tokens",
                "dropped_messages",
                "dropped_evidence",
                "strategy",
            }
            and isinstance(value, (str, int, float, bool))
        }
    return projected


def _workspace_execution_projection(
    run: dict[str, Any], *, events: list[dict[str, Any]], release: dict[str, Any]
) -> dict[str, Any]:
    """Build an owner-safe execution explanation from Runtime's persisted facts.

    The projection deliberately excludes model messages, raw prompts, response bodies, tool
    arguments and tool result content. It exposes identifiers, decisions, counts and bounded
    status facts so a business user can understand what ran without crossing Review data domains.
    """
    result = run.get("result", {}) if isinstance(run.get("result"), dict) else {}
    context = run.get("context", {}) if isinstance(run.get("context"), dict) else {}
    plan = result.get("execution_plan", {})
    plan = plan if isinstance(plan, dict) else {}
    budget = result.get("budget", {})
    budget = budget if isinstance(budget, dict) else {}
    observations = result.get("observations", [])
    observations = observations if isinstance(observations, list) else []

    retrieval = []
    tools = []
    for observation in observations[:100]:
        if not isinstance(observation, dict):
            continue
        if observation.get("type") == "retrieval":
            retrieval.append(
                {
                    key: observation.get(key)
                    for key in (
                        "query",
                        "result_count",
                        "retrieval_profile",
                        "rag_status",
                        "context_truncated",
                        "context_degraded",
                    )
                    if observation.get(key) is not None
                }
            )
        elif observation.get("type") == "tool":
            tool_result = observation.get("result", {})
            tool_result = tool_result if isinstance(tool_result, dict) else {}
            matches = tool_result.get("matches", [])
            tools.append(
                {
                    "tool": str(observation.get("tool", ""))[:200],
                    "success": bool(observation.get("success", False)),
                    "scope": str(tool_result.get("scope", ""))[:200],
                    "match_count": len(matches) if isinstance(matches, list) else None,
                    "error_code": str(observation.get("error_code", ""))[:200],
                }
            )

    timeline = [
        {
            "event_id": str(item.get("event_id", "")),
            "sequence": item.get("sequence", 0),
            "event_type": str(item.get("event_type", "")),
            "occurred_at": item.get("occurred_at"),
            "status": str(item.get("status", "")),
            "error_code": str(item.get("error_code", ""))[:200],
            "metadata": _event_metadata_projection(item.get("metadata")),
        }
        for item in events[:500]
        if isinstance(item, dict) and item.get("run_id") in {"", run.get("run_id")}
    ]
    model_events = [
        item for item in timeline if item["event_type"] == "runtime.request_epoch.pinned"
    ]
    model_routes = list(
        dict.fromkeys(
            str(item["metadata"].get("model_route", ""))
            for item in model_events
            if item["metadata"].get("model_route")
        )
    )
    prompt_versions = list(
        dict.fromkeys(
            str(item["metadata"].get("prompt_version", ""))
            for item in model_events
            if item["metadata"].get("prompt_version")
        )
    )
    route = plan.get("route", {}) if isinstance(plan.get("route"), dict) else {}
    intent = plan.get("intent", {}) if isinstance(plan.get("intent"), dict) else {}
    complexity = plan.get("complexity", {}) if isinstance(plan.get("complexity"), dict) else {}
    context_summary = result.get("context_summary", {})
    context_summary = context_summary if isinstance(context_summary, dict) else {}
    context_status = context_summary.get("status", {})
    context_status = context_status if isinstance(context_status, dict) else {}

    return {
        "release": release,
        "snapshot": {
            "snapshot_id": run.get("snapshot_id", ""),
            "agent_version": context.get("agent_version", ""),
            "graph_version": context.get("graph_version", ""),
            "model_policy_version": context.get("model_policy_version", ""),
        },
        "plan": {
            "plan_id": plan.get("plan_id", ""),
            "plan_stage": plan.get("plan_stage", ""),
            "planner_version": plan.get("planner_version", ""),
            "executor_profile": plan.get("executor_profile", ""),
            "execution_mode": plan.get("execution_mode", ""),
            "topology": plan.get("topology", ""),
            "intent": intent,
            "complexity": complexity,
            "route": route,
            "admission_id": plan.get("admission_id", ""),
            "admission_checks": [
                {
                    "check": item.get("check", ""),
                    "passed": bool(item.get("passed", False)),
                    "reason": str(item.get("reason", ""))[:500],
                }
                for item in (plan.get("admission_checks") or [])[:30]
                if isinstance(item, dict)
            ],
            "allowed_tool_scope": (plan.get("allowed_tool_scope") or [])[:50],
        },
        "retrieval": retrieval,
        "model": {
            "routes": model_routes,
            "model_policy_version": context.get("model_policy_version", ""),
            "prompt_versions": prompt_versions,
            "decision_request_events": sum(
                1 for item in timeline if item["event_type"] == "runtime.model.requested"
            ),
            "billed_llm_calls": budget.get("llm_calls", 0),
            "fallback_chain": route.get("fallback_chain", []),
        },
        "tools": tools,
        "budget": {
            key: budget.get(key)
            for key in (
                "max_steps",
                "max_llm_calls",
                "max_tool_calls",
                "max_retrieval_rounds",
                "max_cost_usd",
                "step_count",
                "llm_calls",
                "tool_calls",
                "retrieval_rounds",
                "spent_cost_usd",
                "attempts_used",
                "deadline_at",
            )
            if budget.get(key) is not None
        },
        "latency_ms": result.get("latency_ms"),
        "context": {
            "selected_history_count": context_summary.get("selected_history_count", 0),
            "rag_status": context_status.get("rag_status", ""),
            "degraded": bool(context_status.get("degraded", False)),
            "degrade_reason": context_status.get("degrade_reason"),
            "token_budget": context_status.get("budget_report", {}),
        },
        "timeline": timeline,
    }


def _workspace_detail_projection(
    run: dict[str, Any],
    *,
    user_id: str,
    artifacts: list[dict[str, Any]],
    events: list[dict[str, Any]],
    release: dict[str, Any],
) -> dict[str, Any]:
    """Reduce an owned Runtime result to a safe but explainable Workspace detail."""
    result = run.get("result", {}) if isinstance(run.get("result"), dict) else {}
    evidence = result.get("evidence", [])
    citations = [
        {
            "evidence_id": item.get("evidence_id", item.get("document_id", "")),
            "source": item.get("source", item.get("title", "")),
        }
        for item in evidence
        if isinstance(item, dict)
    ]
    return {
        "run_id": run.get("run_id"),
        "agent_id": run.get("agent_id") or run.get("context", {}).get("agent_id"),
        "status": run.get("status"),
        "updated_at": run.get("updated_at"),
        "answer": result.get("answer", ""),
        "citations": citations,
        "error_code": run.get("error_code", ""),
        "artifacts": artifacts,
        "execution": _workspace_execution_projection(run, events=events, release=release),
        # Read shares are intentionally not control delegation. Rendering this flag avoids a
        # misleading UI that offers cancel/steering to someone Runtime will correctly reject.
        "can_control": run.get("user_id") == user_id,
        "available_actions": (
            [] if run.get("user_id") != user_id else
            ["approve", "reject", "cancel"]
            if run.get("status") == "WAITING_APPROVAL"
            else ["cancel"]
            if run.get("status") not in {"COMPLETED", "FAILED", "CANCELLED", "LIMIT_EXCEEDED", "REJECTED"}
            else []
        ),
    }


@app.get("/health/ready")
async def ready() -> dict[str, str]:
    """Expose BFF liveness without revealing Runtime or user data."""
    return {"status": "UP", "service": "agent-web-bff"}


@app.get("/api/session")
async def session(request: Request) -> dict[str, Any]:
    """Return the current verified subject projection used to select visible Web workspaces."""
    claims = request.scope.get("auth.claims", {}) if settings.oidc_enabled else {}
    return {
        "tenant_id": request.headers.get("X-Tenant-Id", ""),
        # ``user_id`` is the stable OIDC subject used for ownership and authorization.  A login
        # name can be renamed, so it must never be used to decide who owns a Run or session.
        "user_id": request.headers.get("X-User-Id", ""),
        "username": claims.get("preferred_username", "") if isinstance(claims, dict) else "",
        "roles": [item for item in request.headers.get("X-Roles", "").split(",") if item],
        "permissions": [
            item for item in request.headers.get("X-Permissions", "").split(",") if item
        ],
        "authentication": "oidc" if settings.oidc_enabled else "local-development",
        # Capability does not replace per-route authorization: directory routes still require
        # explicit identity permissions and the protected platform-super-admin role.
        "identity_management_available": _identity_administration_configured(),
        "claim_subject": claims.get("sub", "") if isinstance(claims, dict) else "",
    }


_MANAGEABLE_HUMAN_ROLES = frozenset(
    {"agent-user", "agent-reviewer", "platform-operator", "governance-auditor"}
)
_PROTECTED_HUMAN_ROLE = "platform-super-admin"
_MANAGEABLE_PERMISSIONS = frozenset({
    "rag:read", "rag:ingest:approve", "file:scan", "tool:invoke", "ops:read",
    "release:read", "release:validate", "release:version:publish", "release:create",
    "release:promote", "release:pause", "release:rollback", "model:route:read",
    "model:route:release", "model:route:monitor", "model:route:rollback", "quota:read",
    "quota:write", "audit:export", "audit:export:requeue", "eval:golden:review",
    "agent:review", "run:review:approve", "run:review:assign", "run:review:transfer",
    "run:review:comment", "run:review:label", "evidence:content:read", "run:share",
    "run:tenant:read", "identity:users:read", "identity:users:write",
    "tenant:read", "tenant:write",
})


def _require_platform_super_admin(request: Request) -> None:
    """Guard global directory actions independently of any editable permission checkbox."""
    roles = {item for item in request.headers.get("X-Roles", "").split(",") if item}
    if _PROTECTED_HUMAN_ROLE not in roles:
        raise HTTPException(status_code=403, detail="the platform-super-admin role is required")


async def _require_active_catalog_tenant(request: Request, tenant_id: str) -> None:
    """Reject user assignment to missing/suspended tenants before mutating the IdP record."""
    tenant = await _control_plane(request, "GET", f"/v1/tenants/{quote(tenant_id, safe='')}")
    if tenant.get("status") != "active":
        raise HTTPException(status_code=422, detail="users can be assigned only to an active tenant")


@app.get("/api/console/tenants")
async def list_tenants(request: Request) -> dict[str, Any]:
    """Expose the authoritative Tenant Catalog; only the highest platform role can enumerate it."""
    _require_permission(request, "tenant:read")
    _require_platform_super_admin(request)
    return {"items": await _control_plane(request, "GET", "/v1/tenants")}


@app.post("/api/console/tenants", status_code=201)
async def create_tenant(request: Request) -> dict[str, Any]:
    """Create one tenant and its default policy through Control Plane's transactional boundary."""
    _require_permission(request, "tenant:write")
    _require_platform_super_admin(request)
    payload = await _request_object(request)
    return await _control_plane(request, "POST", "/v1/tenants", json=payload)


@app.put("/api/console/tenants/{tenant_id}")
async def update_tenant(tenant_id: str, request: Request) -> dict[str, Any]:
    """Soft-suspend or retire a tenant; deletion is intentionally excluded to preserve evidence."""
    _require_permission(request, "tenant:write")
    _require_platform_super_admin(request)
    payload = await _request_object(request)
    return await _control_plane(request, "PUT", f"/v1/tenants/{quote(tenant_id, safe='')}", json=payload)


@app.get("/api/console/identity/users")
async def list_identity_users(request: Request) -> dict[str, Any]:
    """Return tenant-scoped human users; the bootstrap super-admin may view every tenant."""
    _require_permission(request, "identity:users:read")
    users = await _identity_admin("GET", "/users", params={"first": 0, "max": 100})
    items = []
    caller_tenant = request.headers.get("X-Tenant-Id", "")
    caller_user_id = request.headers.get("X-User-Id", "")
    caller_roles = {item for item in request.headers.get("X-Roles", "").split(",") if item}
    super_admin = _PROTECTED_HUMAN_ROLE in caller_roles
    for user in users if isinstance(users, list) else []:
        attributes = user.get("attributes") if isinstance(user.get("attributes"), dict) else {}
        role_rows = await _identity_admin("GET", f"/users/{user['id']}/role-mappings/realm")
        all_roles = {row.get("name", "") for row in role_rows}
        username = user.get("username", "")
        tenant_id = (attributes.get("tenant_id") or [""])[0]
        # Workload service accounts are never assignable through the human authorization screen.
        if username.startswith("service-account-") or "platform-workload" in all_roles:
            continue
        if not super_admin and tenant_id != caller_tenant:
            continue
        roles = sorted(
            row.get("name", "") for row in role_rows if row.get("name", "") in _MANAGEABLE_HUMAN_ROLES
        )
        items.append({
            # ``identity_id`` is Keycloak's management identifier. ``user_id`` is OIDC subject
            # and normally equal to it; username remains presentation/login-only.
            "identity_id": user["id"], "user_id": user["id"], "username": username, "enabled": bool(user.get("enabled")),
            "email": user.get("email", ""), "tenant_id": tenant_id,
            "permissions": sorted(attributes.get("permissions") or []), "roles": roles,
            "current": user["id"] == caller_user_id,
            "protected": _PROTECTED_HUMAN_ROLE in all_roles,
        })
    return {
        "items": items, "roles": sorted(_MANAGEABLE_HUMAN_ROLES),
        "permissions": sorted(_MANAGEABLE_PERMISSIONS), "super_admin": super_admin,
    }


async def _assignable_reviewers(request: Request) -> list[dict[str, str]]:
    """Resolve the only people that may receive a Review Assignment in this tenant.

    Login names are presentation-only.  The Runtime stores the immutable OIDC subject as
    ``reviewer_id`` so a later username change cannot silently move an existing assignment.
    This server-side directory check is deliberately independent of the browser select list.
    """
    tenant_id = request.headers.get("X-Tenant-Id", "").strip()
    if not tenant_id:
        raise HTTPException(status_code=401, detail="verified tenant identity is required")
    users = await _identity_admin("GET", "/users", params={"first": 0, "max": 100})
    reviewers: list[dict[str, str]] = []
    for user in users if isinstance(users, list) else []:
        identity_id = str(user.get("id", "")).strip()
        attributes = user.get("attributes") if isinstance(user.get("attributes"), dict) else {}
        user_tenant = str((attributes.get("tenant_id") or [""])[0]).strip()
        permissions = {str(value).strip() for value in attributes.get("permissions") or []}
        if not identity_id or not bool(user.get("enabled")) or user_tenant != tenant_id:
            continue
        role_rows = await _identity_admin("GET", f"/users/{identity_id}/role-mappings/realm")
        roles = {str(row.get("name", "")).strip() for row in role_rows if isinstance(row, dict)}
        # Service identities and disabled/cross-tenant accounts can never become human reviewers.
        if (
            str(user.get("username", "")).startswith("service-account-")
            or "platform-workload" in roles
            or "agent-reviewer" not in roles
            or "agent:review" not in permissions
        ):
            continue
        reviewers.append({"user_id": identity_id, "username": str(user.get("username", ""))})
    return sorted(reviewers, key=lambda item: (item["username"].lower(), item["user_id"]))


@app.get("/api/workspace/reviewers")
async def list_assignable_reviewers(request: Request) -> dict[str, Any]:
    """Return a tenant-scoped reviewer directory for the assignment picker.

    Only an authorized assigner can enumerate this reduced directory; ordinary users cannot use
    the endpoint to discover colleagues or reviewer identities.
    """
    _require_permission(request, "run:review:assign")
    return {"items": await _assignable_reviewers(request)}


@app.post("/api/workspace/runs/{run_id}/review-assignment", status_code=204)
async def assign_workspace_review(request: Request, run_id: str) -> Response:
    """Validate a human reviewer against IdP, then create the Runtime Assignment.

    The Runtime persists the assignment and its Governance Outbox event in one transaction.  The
    IdP is consulted here because it is the authority for tenant membership, enabled status,
    human roles, and effective user permissions; a select value alone is never trusted.
    """
    _require_permission(request, "run:review:assign")
    _require_high_risk_authentication(request)
    payload = await _request_object(request)
    reviewer_id = str(payload.get("reviewer_id", "")).strip()
    reason = str(payload.get("reason", "")).strip()
    if not 2 <= len(reviewer_id) <= 160 or not 2 <= len(reason) <= 2_000:
        raise HTTPException(status_code=422, detail="reviewer_id and reason must be between 2 and 2000 characters")
    reviewer_ids = {item["user_id"] for item in await _assignable_reviewers(request)}
    if reviewer_id not in reviewer_ids:
        raise HTTPException(status_code=422, detail="reviewer must be an enabled reviewer in the current tenant")
    await _runtime(
        request,
        "POST",
        f"/agent/runs/{quote(run_id, safe='')}/review-assignments",
        json={"reviewer_id": reviewer_id, "reason": reason},
    )
    return Response(status_code=204)


@app.put("/api/console/identity/users/{identity_id}")
async def update_identity_user(identity_id: str, request: Request) -> dict[str, Any]:
    """Apply a reasoned tenant/role/permission change; passwords and service roles are excluded."""
    _require_permission(request, "identity:users:write")
    payload = await _request_object(request)
    reason = str(payload.get("reason", "")).strip()
    tenant_id = str(payload.get("tenant_id", "")).strip()
    roles = {str(value) for value in payload.get("roles", [])}
    permissions = sorted({str(value).strip() for value in payload.get("permissions", []) if str(value).strip()})
    if (
        not reason or not tenant_id or not roles.issubset(_MANAGEABLE_HUMAN_ROLES)
        or not set(permissions).issubset(_MANAGEABLE_PERMISSIONS)
    ):
        raise HTTPException(status_code=422, detail="tenant, reason, and catalog roles/permissions are required")
    current = await _identity_admin("GET", f"/users/{identity_id}")
    current_roles = await _identity_admin("GET", f"/users/{identity_id}/role-mappings/realm")
    existing_role_names = {row.get("name", "") for row in current_roles}
    current_attributes = current.get("attributes") if isinstance(current.get("attributes"), dict) else {}
    current_tenant = (current_attributes.get("tenant_id") or [""])[0]
    caller_tenant = request.headers.get("X-Tenant-Id", "")
    caller_user_id = request.headers.get("X-User-Id", "")
    caller_roles = {item for item in request.headers.get("X-Roles", "").split(",") if item}
    super_admin = _PROTECTED_HUMAN_ROLE in caller_roles
    if current.get("username", "").startswith("service-account-") or "platform-workload" in existing_role_names:
        raise HTTPException(status_code=403, detail="workload identities cannot be managed here")
    if not super_admin and current_tenant != caller_tenant:
        raise HTTPException(status_code=404, detail="identity user was not found in the current tenant")
    if _PROTECTED_HUMAN_ROLE in existing_role_names:
        raise HTTPException(status_code=403, detail="the bootstrap super-admin is protected from browser changes")
    await _require_active_catalog_tenant(request, tenant_id)
    if current.get("id") == caller_user_id and (
        not bool(payload.get("enabled", current.get("enabled", True)))
        or "identity:users:write" not in permissions
    ):
        raise HTTPException(status_code=422, detail="an administrator cannot disable or lock out the active account")
    current["enabled"] = bool(payload.get("enabled", current.get("enabled", True)))
    current["attributes"] = {"tenant_id": [tenant_id], "permissions": permissions}
    await _identity_admin("PUT", f"/users/{identity_id}", json=current)
    removable = [row for row in current_roles if row.get("name") in _MANAGEABLE_HUMAN_ROLES]
    if removable:
        await _identity_admin("DELETE", f"/users/{identity_id}/role-mappings/realm", json=removable)
    if roles:
        available = await _identity_admin("GET", "/roles")
        selected = [row for row in available if row.get("name") in roles]
        await _identity_admin("POST", f"/users/{identity_id}/role-mappings/realm", json=selected)
    # Keycloak records the protected resource mutation in its administrative audit stream.
    await _identity_admin("GET", f"/users/{identity_id}")
    # Existing access tokens contain the old claims. Revoking the target user's IdP sessions makes
    # the new role/permission set effective on their next request instead of hours later.
    await _identity_admin("POST", f"/users/{identity_id}/logout")
    return {"status": "updated", "identity_id": identity_id, "reason": reason, "changed_at": datetime.now(UTC).isoformat(), "request_id": f"identity_{uuid4().hex}"}


@app.get("/api/workspace/runs")
async def list_workspace_runs(request: Request, limit: int = 8, page: int = 1) -> dict[str, Any]:
    """Project only the current user's task list; no target-user filter exists."""
    bounded_limit = min(max(limit, 1), 100)
    bounded_page = min(max(page, 1), 1_251)
    return await _runtime(
        request, "GET", "/agent/runs",
        params={"limit": bounded_limit, "offset": (bounded_page - 1) * bounded_limit},
    )


@app.get("/api/workspace/model-routes")
async def workspace_model_routes(
    request: Request, agent_id: str, environment: str, session_id: str
) -> dict[str, Any]:
    """Expose only the selected Release's logical model routes to the task form.

    The Runtime resolves and pins the Release using ``session_id``.  This proxy intentionally
    does not return provider credentials, base URLs, vendor revisions, or arbitrary Gateway
    catalog entries; the browser can choose only a route declared by the Agent Snapshot.
    """
    if not 2 <= len(agent_id.strip()) <= 160:
        raise HTTPException(status_code=422, detail="agent_id is invalid")
    if not 2 <= len(environment.strip()) <= 64 or not 8 <= len(session_id.strip()) <= 160:
        raise HTTPException(status_code=422, detail="environment or session_id is invalid")
    return await _runtime(
        request,
        "GET",
        "/agent/model-routes",
        params={"agent_id": agent_id, "environment": environment, "session_id": session_id},
    )


@app.get("/api/review/runs")
async def list_review_runs(request: Request, limit: int = 30) -> dict[str, Any]:
    """Return only Runs explicitly assigned to the verified reviewer.

    Runtime, rather than this UI layer, verifies ``agent:review`` and the assignment relation.
    This prevents a browser parameter such as ``reviewer_id`` from becoming a tenant-wide data
    export primitive.
    """
    return await _runtime(request, "GET", "/agent/review/runs", params={"limit": limit})


@app.get("/api/review/runs/{run_id}")
async def review_run(request: Request, run_id: str) -> dict[str, Any]:
    """Proxy the Runtime review projection after its assignment-level authorization check."""
    return await _runtime(request, "GET", f"/agent/review/runs/{run_id}")


@app.post("/api/review/runs/{run_id}/approval")
async def review_approval(request: Request, run_id: str) -> dict[str, Any]:
    """Submit one reviewer decision to Runtime's existing approval inbox and state machine."""
    payload = await _request_object(request)
    return await _runtime(request, "POST", f"/agent/runs/{run_id}/resume", json=payload)


@app.post("/api/review/runs/{run_id}/transfer", status_code=204)
async def review_transfer(request: Request, run_id: str) -> Response:
    """Transfer only the caller's explicit Review Assignment through Runtime's atomic store operation."""
    payload = await _request_object(request)
    await _runtime(request, "POST", f"/agent/review/runs/{run_id}/transfer", json=payload)
    return Response(status_code=204)


@app.post("/api/review/runs/{run_id}/collaborators", status_code=204)
async def add_review_collaborator(request: Request, run_id: str) -> Response:
    """新增共同审查人而不撤销当前 reviewer；Runtime 验证 Assignment 与细粒度权限。"""
    await _runtime(
        request, "POST", f"/agent/review/runs/{run_id}/collaborators", json=await _request_object(request)
    )
    return Response(status_code=204)


@app.get("/api/review/runs/{run_id}/evidence/{evidence_id}")
async def review_evidence(request: Request, run_id: str, evidence_id: str) -> dict[str, Any]:
    """代理 Runtime 的 Assignment + data-domain 双重授权证据正文投影。"""
    return await _runtime(
        request, "GET", f"/agent/review/runs/{run_id}/evidence/{evidence_id}"
    )


@app.get("/api/review/runs/{run_id}/comments")
async def review_comments(request: Request, run_id: str) -> dict[str, Any]:
    """读取 Runtime 已按 Assignment 授权的协作备注，BFF 不单独维护评论副本。"""
    return await _runtime(request, "GET", f"/agent/review/runs/{run_id}/comments")


@app.post("/api/review/runs/{run_id}/comments", status_code=201)
async def create_review_comment(request: Request, run_id: str) -> dict[str, Any]:
    """将当前审查人的备注交给 Runtime 写入，以转交后的 Assignment 为最终写入边界。"""
    _require_permission(request, "run:review:comment")
    return await _runtime(
        request, "POST", f"/agent/review/runs/{run_id}/comments", json=await _request_object(request)
    )


@app.post("/api/review/runs/{run_id}/feedback")
async def review_feedback(request: Request, run_id: str) -> dict[str, Any]:
    """Write an assigned expert's label to Governance feedback, never into the Run record."""
    _require_permission(request, "run:review:label")
    # Runtime confirms the reviewer-to-Run relation before Governance receives any feedback.
    await _runtime(request, "GET", f"/agent/review/runs/{run_id}")
    payload = await _request_object(request)
    rating = _form_number(payload, "rating", integer=True, maximum=5)
    if rating < 1 or rating > 5:
        raise HTTPException(status_code=422, detail="rating must be between 1 and 5")
    headers = _governance_headers(request)
    body = {
        "requestId": run_id,
        "rating": rating,
        "reviewStatus": str(payload.get("review_status", "REVIEWED")),
        "criticality": str(payload.get("criticality", "normal")),
        "expectedAnswer": str(payload.get("expected_answer", ""))[:12_000],
        "tags": [item[:120] for item in _form_strings(payload, "tags", 20)],
        "source": "agent-web-review",
    }
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, **_mtls_options()) as client:
            response = await client.post(
                f"{settings.governance_base_url.rstrip('/')}/v1/governance/evaluations/feedback",
                headers=headers,
                json=body,
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="governance feedback is unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text[:2_000])
    return response.json()


@app.get("/api/console/golden-candidates")
async def console_golden_candidates(request: Request) -> dict[str, Any]:
    """提供 Console 的候选 Golden 审核队列，不代理整个线上样本池。"""
    _require_permission(request, "eval:golden:review")
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, **_mtls_options()) as client:
            response = await client.get(
                f"{settings.governance_base_url.rstrip('/')}/v1/governance/evaluations/online/golden-candidates",
                headers=_governance_headers(request),
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="governance candidates are unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text[:2_000])
    return response.json()


@app.post("/api/console/golden-candidates/{candidate_id}/review")
async def review_console_golden_candidate(request: Request, candidate_id: str) -> dict[str, Any]:
    """批准或拒绝候选 Golden；Governance 仍是写入 Golden Case 的唯一所有者。"""
    _require_permission(request, "eval:golden:review")
    payload = await _request_object(request)
    if not isinstance(payload.get("approved"), bool):
        raise HTTPException(status_code=422, detail="approved must be a boolean")
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, **_mtls_options()) as client:
            response = await client.post(
                f"{settings.governance_base_url.rstrip('/')}/v1/governance/evaluations/online/golden-candidates/{candidate_id}/review",
                headers=_governance_headers(request),
                json={
                    "approved": payload["approved"],
                    "note": str(payload.get("note", ""))[:2_000],
                    "groundTruth": str(payload.get("ground_truth", ""))[:12_000],
                },
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="governance candidate review is unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text[:2_000])
    return response.json()


async def _health_projection(name: str, url: str) -> dict[str, str | int]:
    """Read one public readiness endpoint without forwarding browser credentials or error bodies."""
    try:
        async with httpx.AsyncClient(
            timeout=min(settings.request_timeout_seconds, 5.0), **_mtls_options()
        ) as client:
            response = await client.get(url)
        return {
            "service": name,
            "status": "UP" if response.is_success else "DOWN",
            "http_status": response.status_code,
        }
    except httpx.RequestError:
        # Do not return hostnames, stack traces or upstream exception details to a browser Console.
        return {"service": name, "status": "DOWN", "http_status": 0}


@app.get("/api/console/services")
async def console_services(request: Request) -> dict[str, Any]:
    """Return an operations-only, read-only readiness overview of the seven service boundaries."""
    _require_permission(request, "ops:read")
    targets = {
        "Agent Runtime": settings.console_runtime_health_url,
        "Control Plane": settings.console_control_plane_health_url,
        "Governance": settings.console_governance_health_url,
        "LLM Gateway": settings.console_llm_gateway_health_url,
        "Context Service": settings.console_context_health_url,
        "RAG Service": settings.console_rag_health_url,
        "Ingestion": settings.console_ingestion_health_url,
        "Tool Gateway": settings.console_tool_gateway_health_url,
    }
    items = await asyncio.gather(*(_health_projection(name, url) for name, url in targets.items()))
    return {"items": items}


@app.get("/api/console/agents")
async def console_agents(
    request: Request,
    limit: int = Query(default=8, ge=1, le=100),
    page: int = Query(default=1, ge=1, le=12_501),
) -> dict[str, Any]:
    """分页列出当前租户的 Agent 发布目录，不在浏览器端做全量切片。"""
    _require_permission(request, "release:read")
    offset = (page - 1) * limit
    result = await _control_plane(
        request, "GET", "/v1/agents/catalog", params={"limit": limit, "offset": offset}
    )
    agents = result.get("items", []) if isinstance(result, dict) else []
    return {
        "items": [
            {
                "agent_id": item.get("agent_id", ""),
                "revision": item.get("revision", 0),
                "updated_at": item.get("updated_at", ""),
            }
            for item in agents
            if isinstance(item, dict)
        ],
        "total_items": int(result.get("total_items", 0)) if isinstance(result, dict) else 0,
        "limit": int(result.get("limit", limit)) if isinstance(result, dict) else limit,
        "page": page,
    }


@app.get("/api/console/agents/{agent_id}/releases")
async def console_agent_releases(
    request: Request, agent_id: str, environment: str | None = None
) -> dict[str, Any]:
    """读取一个 Agent 的发布清单摘要，不把 Draft 或 Snapshot 正文塞进浏览器列表。"""
    _require_permission(request, "release:read")
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, **_mtls_options()) as client:
            response = await client.get(
                f"{settings.control_plane_base_url.rstrip('/')}/v1/agents/{agent_id}/releases",
                headers=_control_plane_headers(request),
                params={"environment": environment} if environment else None,
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="control plane is unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text[:2_000])
    releases = response.json()
    return {
        "items": [
            {
                "release_id": item.get("release_id", ""),
                "environment": item.get("environment", ""),
                "status": item.get("status", ""),
                "version_id": item.get("version_id", ""),
                "snapshot_id": item.get("snapshot_id", ""),
                "created_at": item.get("created_at", ""),
            }
            for item in releases
            if isinstance(item, dict)
        ]
    }


@app.get("/api/console/agents/{agent_id}/versions")
async def console_agent_versions(request: Request, agent_id: str) -> dict[str, Any]:
    """Expose immutable Version metadata for release selection, never its embedded snapshot body."""
    _require_permission(request, "release:read")
    versions = await _control_plane(request, "GET", f"/v1/agents/{agent_id}/versions")
    return {
        "items": [
            {
                "version_id": item.get("version_id", ""),
                "semantic_version": item.get("semantic_version", ""),
                "source_revision": item.get("source_revision", 0),
                "content_hash": item.get("content_hash", ""),
                "change_summary": item.get("change_summary", ""),
                "published_by": item.get("published_by", ""),
                "published_at": item.get("published_at", ""),
            }
            for item in versions
            if isinstance(item, dict)
        ]
    }


@app.post("/api/console/agents/{agent_id}/validate")
async def console_validate_agent(request: Request, agent_id: str) -> dict[str, Any]:
    """发布前只读校验 Draft；该动作不会创建 Version 或改变 Runtime 流量。"""
    _require_permission(request, "release:validate")
    return await _control_plane(request, "POST", f"/v1/agents/{agent_id}/validate", json={})


@app.post("/api/console/agents/{agent_id}/versions")
async def console_publish_agent_version(request: Request, agent_id: str) -> dict[str, Any]:
    """冻结一个不可变 Agent Version；语义版本和变更摘要由操作者明确提交。"""
    _require_permission(request, "release:version:publish")
    _require_high_risk_authentication(request)
    payload = await _request_object(request)
    semantic_version = str(payload.get("semantic_version", ""))
    if not semantic_version or len(semantic_version) > 80:
        raise HTTPException(status_code=422, detail="semantic_version is required")
    return await _control_plane(
        request,
        "POST",
        f"/v1/agents/{agent_id}/versions",
        json={
            "semantic_version": semantic_version,
            "change_summary": str(payload.get("change_summary", ""))[:2_000],
        },
    )


@app.post("/api/console/agents/{agent_id}/releases")
async def console_create_release(request: Request, agent_id: str) -> dict[str, Any]:
    """由冻结 Version 创建候选 Release，质量门禁仍由 Control Plane 强制执行。"""
    _require_permission(request, "release:create")
    _require_high_risk_authentication(request)
    payload = await _request_object(request)
    if str(payload.get("confirm_agent_id", "")) != agent_id:
        raise HTTPException(status_code=422, detail="confirm_agent_id must match the target Agent")
    return await _control_plane(
        request,
        "POST",
        f"/v1/agents/{agent_id}/releases",
        json={
            "version_id": str(payload.get("version_id", "")),
            "environment": str(payload.get("environment", "production")),
            "rollout_percentage": _form_number(payload, "rollout_percentage", integer=True, maximum=100),
            "tenant_allowlist": _form_strings(payload, "tenant_allowlist", 100),
            "reason": str(payload.get("reason", ""))[:2_000],
            "quality_gate_run_id": payload.get("quality_gate_run_id") or None,
            "agent_lab_experiment_id": payload.get("agent_lab_experiment_id") or None,
        },
    )


async def _release_control_action(
    request: Request, release_id: str, action: str, permission: str
) -> dict[str, Any]:
    """执行单个 Release 高风险动作，并用回显 ID 防止误操作错误目标。"""
    _require_permission(request, permission)
    _require_high_risk_authentication(request)
    payload = await _request_object(request)
    if str(payload.get("confirm_release_id", "")) != release_id:
        raise HTTPException(status_code=422, detail="confirm_release_id must match the target Release")
    body = (
        {"rollout_percentage": _form_number(payload, "rollout_percentage", 100, integer=True, maximum=100)}
        if action == "promote"
        else {}
    )
    return await _control_plane(
        request, "POST", f"/v1/releases/{release_id}/{action}", json=body
    )


@app.post("/api/console/releases/{release_id}/promote")
async def console_promote_release(request: Request, release_id: str) -> dict[str, Any]:
    """提升 Release 流量；Control Plane 再校验质量门禁、状态和 CAS revision。"""
    return await _release_control_action(request, release_id, "promote", "release:promote")


@app.post("/api/console/releases/{release_id}/pause")
async def console_pause_release(request: Request, release_id: str) -> dict[str, Any]:
    """暂停 Active Release；冻结快照保持不可变。"""
    return await _release_control_action(request, release_id, "pause", "release:pause")


@app.post("/api/console/releases/{release_id}/rollback")
async def console_rollback_release(request: Request, release_id: str) -> dict[str, Any]:
    """回滚到 Control Plane 选出的兼容历史 Release，而非浏览器指定任意快照。"""
    return await _release_control_action(request, release_id, "rollback", "release:rollback")


@app.get("/api/console/connector-artifact-dlq")
async def console_connector_artifact_dlq(request: Request) -> dict[str, Any]:
    """读取当前租户 Connector Artifact 死信摘要，不返回结果正文。"""
    _require_permission(request, "connector:artifact:dlq:read")
    return await _runtime(request, "GET", "/agent/connectors/artifact-outbox/dead-letters")


@app.post("/api/console/connector-artifact-dlq/{outbox_id}/requeue")
async def console_requeue_connector_artifact(request: Request, outbox_id: str) -> dict[str, Any]:
    """显式重排一条当前租户死信；调用者必须回显目标 ID。"""
    _require_permission(request, "connector:artifact:dlq:requeue")
    _require_high_risk_authentication(request)
    payload = await _request_object(request)
    if str(payload.get("confirm_outbox_id", "")) != outbox_id:
        raise HTTPException(status_code=422, detail="confirm_outbox_id must match the target record")
    return await _runtime(
        request,
        "POST",
        f"/agent/connectors/artifact-outbox/dead-letters/{outbox_id}/requeue",
        json={},
    )


@app.get("/api/console/model-route-releases")
async def console_model_route_releases(request: Request) -> dict[str, Any]:
    """List Control Plane-owned canary releases; Gateway runtime overrides remain hidden."""
    _require_permission(request, "model:route:read")
    items = await _control_plane(request, "GET", "/v1/model-route-releases")
    return {"items": items if isinstance(items, list) else []}


@app.post("/api/console/model-route-releases")
async def console_create_model_route_release(request: Request) -> dict[str, Any]:
    """Start a quality-gated model canary instead of mutating Gateway routes directly."""
    _require_permission(request, "model:route:release")
    _require_high_risk_authentication(request)
    payload = await _request_object(request)
    route_name = str(payload.get("routeName", "")).strip()
    if not route_name or str(payload.get("confirmRouteName", "")) != route_name:
        raise HTTPException(status_code=422, detail="confirmRouteName must match routeName")
    return await _control_plane(
        request,
        "POST",
        "/v1/model-route-releases",
        json={
            "routeName": route_name,
            "canaryTarget": str(payload.get("canaryTarget", ""))[:200],
            "judgeRunId": str(payload.get("judgeRunId", ""))[:200],
            "canaryPercent": _form_number(payload, "canaryPercent", 5, integer=True, maximum=100),
            "modelLabExperimentId": str(payload.get("modelLabExperimentId", ""))[:200],
        },
    )


@app.post("/api/console/model-route-releases/{release_id}/monitor")
async def console_monitor_model_route_release(request: Request, release_id: str) -> dict[str, Any]:
    """Request one governed canary observation without bypassing Control Plane decisions."""
    _require_permission(request, "model:route:monitor")
    return await _control_plane(
        request, "POST", f"/v1/model-route-releases/{release_id}/monitor", json={}
    )


@app.post("/api/console/model-route-releases/{release_id}/rollback")
async def console_rollback_model_route_release(request: Request, release_id: str) -> dict[str, Any]:
    """Restore the recorded pre-canary route after ID confirmation and strong authentication."""
    _require_permission(request, "model:route:rollback")
    _require_high_risk_authentication(request)
    payload = await _request_object(request)
    if str(payload.get("confirmReleaseId", "")) != release_id:
        raise HTTPException(status_code=422, detail="confirmReleaseId must match release_id")
    return await _control_plane(
        request,
        "POST",
        f"/v1/model-route-releases/{release_id}/rollback",
        json={"reason": str(payload.get("reason", "manual rollback"))[:2_000]},
    )


@app.get("/api/console/llm-quotas")
async def console_llm_quotas(request: Request) -> dict[str, Any]:
    """Read the Control Plane tenant policy and expose only its LLM quota projection."""
    _require_permission(request, "quota:read")
    policy = await _control_plane(request, "GET", "/v1/tenant-policy")
    return {
        "items": [
            {"subject": subject, **quota}
            for subject, quota in (policy.get("llm_quotas", {}) or {}).items()
        ],
        "policy_updated_at": policy.get("updated_at", ""),
    }


@app.put("/api/console/llm-quotas/{subject}")
async def console_update_llm_quota(request: Request, subject: str) -> dict[str, Any]:
    """Update one desired quota through a full Control Plane policy snapshot and Gateway Saga."""
    _require_permission(request, "quota:write")
    _require_high_risk_authentication(request)
    if not subject or len(subject) > 160 or ":" in subject:
        raise HTTPException(status_code=422, detail="subject must be '*' or a local user ID")
    payload = await _request_object(request)
    if str(payload.get("confirmSubject", "")) != subject:
        raise HTTPException(status_code=422, detail="confirmSubject must match subject")
    policy = await _control_plane(request, "GET", "/v1/tenant-policy")
    quotas = dict(policy.get("llm_quotas", {}) or {})
    quotas[subject] = {
        "daily_token_limit": _form_number(payload, "dailyTokenLimit", integer=True),
        "daily_cost_limit_usd": _form_number(payload, "dailyCostLimitUsd"),
        "currency": "USD",
    }
    updated = await _control_plane(
        request,
        "PUT",
        "/v1/tenant-policy",
        json={
            "allowed_models": policy.get("allowed_models", []),
            "allowed_data_regions": policy.get("allowed_data_regions", []),
            "max_canary_percentage": policy.get("max_canary_percentage", 100),
            "require_approval_for_high_risk_tools": policy.get(
                "require_approval_for_high_risk_tools", True
            ),
            "llm_quotas": quotas,
        },
    )
    return {"subject": subject, "quota": updated.get("llm_quotas", {}).get(subject, {})}


@app.get("/api/console/audit-exports")
async def console_audit_exports(request: Request) -> dict[str, Any]:
    """List current-tenant WORM jobs without revealing credentials or export bodies."""
    _require_permission(request, "audit:export")
    return await _governance(request, "GET", "/v1/governance/audit-exports")


@app.post("/api/console/audit-exports")
async def console_create_audit_export(request: Request) -> dict[str, Any]:
    """Queue a retention-locked audit export after explicit strong authentication."""
    _require_permission(request, "audit:export")
    _require_high_risk_authentication(request)
    payload = await _request_object(request)
    tenant_id = request.headers.get("X-Tenant-Id", "")
    if str(payload.get("confirmTenantId", "")) != tenant_id:
        raise HTTPException(status_code=422, detail="confirmTenantId must match current tenant")
    return await _governance(request, "POST", "/v1/governance/audit-exports", json={})


@app.post("/api/console/audit-exports/{job_id}/requeue")
async def console_requeue_audit_export(request: Request, job_id: str) -> dict[str, Any]:
    """Explicitly replay one failed export; silent automatic DLQ replay is forbidden."""
    _require_permission(request, "audit:export:requeue")
    _require_high_risk_authentication(request)
    payload = await _request_object(request)
    if str(payload.get("confirmJobId", "")) != job_id:
        raise HTTPException(status_code=422, detail="confirmJobId must match target job")
    return await _governance(
        request, "POST", f"/v1/governance/audit-exports/{job_id}/requeue", json={}
    )


@app.post("/api/workspace/runs")
async def create_workspace_run(request: Request) -> dict[str, Any]:
    """Submit an interactive Workspace task while preserving Runtime's release resolution."""
    payload = await _request_object(request)
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=422, detail="metadata must be a JSON object")
    payload["metadata"] = {**metadata, "interaction_channel": "agent-web"}
    return await _runtime(request, "POST", "/agent/interactive-runs", json=payload)


async def _workspace_release_binding(
    request: Request, *, agent_id: str, snapshot_id: str
) -> dict[str, Any]:
    """Resolve the Release that supplied an owned Run's frozen version.

    This is a read-only projection for a Run the Runtime has already authorized. The browser does
    not receive Control Plane credentials and cannot select a different Release through this path.
    Historical data remains readable when Control Plane is temporarily unavailable.
    """
    if not agent_id or not snapshot_id:
        return {"status": "unavailable", "reason": "run has no release binding"}
    try:
        releases = await _control_plane(request, "GET", f"/v1/agents/{agent_id}/releases")
    except HTTPException:
        return {"status": "unavailable", "reason": "control plane is unavailable"}
    items = releases if isinstance(releases, list) else releases.get("items", [])
    matched = next(
        (
            item
            for item in items
            if isinstance(item, dict) and str(item.get("version_id", "")) == snapshot_id
        ),
        None,
    )
    if matched is None:
        return {
            "status": "unresolved",
            "version_id": snapshot_id,
            "reason": "no retained Release references this frozen version",
        }
    return {
        "release_id": matched.get("release_id", ""),
        "environment": matched.get("environment", ""),
        "status": matched.get("status", ""),
        "version_id": matched.get("version_id", ""),
        "snapshot_id": matched.get("snapshot_id", ""),
        "created_at": matched.get("created_at"),
    }


@app.get("/api/workspace/runs/{run_id}")
async def workspace_run(request: Request, run_id: str) -> dict[str, Any]:
    """Read one owned Run; Runtime returns 404 for foreign resources."""
    run = await _runtime(request, "GET", f"/agent/runs/{run_id}")
    context = run.get("context", {}) if isinstance(run.get("context"), dict) else {}
    session_id = str(context.get("session_id", ""))
    events: list[dict[str, Any]] = []
    if session_id:
        try:
            event_index = await _runtime(
                request,
                "GET",
                f"/agent/sessions/{session_id}/events",
                params={"limit": 500},
            )
            events = event_index.get("events", [])
        except HTTPException:
            # Event history enriches explainability but must not hide an otherwise readable result.
            events = []
    release = await _workspace_release_binding(
        request,
        agent_id=str(run.get("agent_id") or context.get("agent_id", "")),
        snapshot_id=str(run.get("snapshot_id", "")),
    )
    try:
        artifact_index = await _runtime(request, "GET", f"/agent/runs/{run_id}/artifacts")
        artifacts = artifact_index.get("items", [])
        ingestion_index = await _runtime(
            request, "GET", f"/agent/runs/{run_id}/artifact-ingestions"
        )
        ingestion_by_artifact = {}
        for item in ingestion_index.get("items", []):
            projected = dict(item)
            projected["approval_status"] = str(item.get("status") or "")
            if item.get("downstream_status"):
                projected["status"] = (
                    f"{projected['approval_status']} / INDEX_{item['downstream_status']}"
                )
            ingestion_by_artifact[item.get("artifact_id")] = projected
        artifacts = [
            {**item, "rag_ingestion": ingestion_by_artifact.get(item.get("artifact_id"))}
            for item in artifacts
        ]
    except HTTPException as exc:
        # A Context outage must not hide the Run result. The UI can distinguish an empty index
        # from an unavailable one without learning any upstream infrastructure details.
        artifacts = [{"status": "unavailable"}] if exc.status_code == 503 else []
    return _workspace_detail_projection(
        run,
        user_id=request.headers.get("X-User-Id", ""),
        artifacts=artifacts,
        events=events,
        release=release,
    )


@app.post(
    "/api/workspace/runs/{run_id}/artifacts/{artifact_id}/ingestion-decision"
)
async def workspace_artifact_ingestion_decision(
    request: Request, run_id: str, artifact_id: str
) -> dict[str, Any]:
    """Forward a permissioned, ID-confirmed decision; Web never submits RAG content."""
    _require_permission(request, "rag:ingest:approve")
    _require_high_risk_authentication(request)
    payload = await _request_object(request)
    if str(payload.get("confirm_artifact_id", "")) != artifact_id:
        raise HTTPException(status_code=422, detail="confirm_artifact_id must match artifact_id")
    return await _runtime(
        request,
        "POST",
        f"/agent/runs/{run_id}/artifact-ingestions/{artifact_id}/decision",
        json=payload,
    )


@app.get("/api/workspace/runs/{run_id}/artifacts/{artifact_id}/download")
async def workspace_artifact_download(
    request: Request, run_id: str, artifact_id: str
) -> RedirectResponse:
    """重定向到 Runtime 已授权的短期数据面 URL；URL 不写入页面 JSON、日志或 BFF 状态。"""
    payload = await _runtime(
        request, "GET", f"/agent/runs/{run_id}/artifacts/{artifact_id}/download"
    )
    url = str(payload.get("url", ""))
    if not url.startswith(("https://", "http://")):
        raise HTTPException(status_code=503, detail="artifact delivery returned an invalid URL")
    return RedirectResponse(url=url, status_code=307)


@app.get("/api/workspace/runs/{run_id}/artifacts/{artifact_id}/preview")
async def workspace_artifact_preview(
    request: Request, run_id: str, artifact_id: str, max_chars: int = 50_000
) -> dict[str, Any]:
    """Proxy the bounded Runtime preview; object references and signed URLs stay upstream."""
    return await _runtime(
        request,
        "GET",
        f"/agent/runs/{run_id}/artifacts/{artifact_id}/preview",
        params={"max_chars": min(max(max_chars, 256), 100_000)},
    )


@app.get(
    "/api/workspace/runs/{run_id}/artifacts/{artifact_id}/compare/{base_artifact_id}"
)
async def workspace_artifact_compare(
    request: Request,
    run_id: str,
    artifact_id: str,
    base_artifact_id: str,
    max_chars: int = 80_000,
) -> dict[str, Any]:
    """Return a bounded same-series diff after Runtime revalidates the Run relationship."""
    return await _runtime(
        request,
        "GET",
        f"/agent/runs/{run_id}/artifacts/{artifact_id}/compare/{base_artifact_id}",
        params={"max_chars": min(max(max_chars, 1_000), 100_000)},
    )


@app.post("/api/workspace/runs/{run_id}/cancel")
async def cancel_workspace_run(request: Request, run_id: str) -> dict[str, Any]:
    """Delegate cancellation to Runtime's persisted state machine."""
    return await _runtime(request, "POST", f"/agent/runs/{run_id}/cancel", json={})


@app.post("/api/workspace/runs/{run_id}/shares", status_code=204)
async def share_workspace_run(request: Request, run_id: str) -> Response:
    """Proxy an owner-only, read-only Run share; Runtime enforces ownership and scope."""
    await _runtime(request, "POST", f"/agent/runs/{run_id}/shares", json=await _request_object(request))
    return Response(status_code=204)


@app.post("/api/workspace/runs/{run_id}/inputs", status_code=202)
async def steer_workspace_run(request: Request, run_id: str) -> dict[str, Any]:
    """Submit owner steering through Runtime's persisted mailbox, never via browser-local state."""
    run = await _runtime(request, "GET", f"/agent/runs/{run_id}")
    if run.get("status") in {"COMPLETED", "FAILED", "CANCELLED", "LIMIT_EXCEEDED", "REJECTED"}:
        raise HTTPException(
            status_code=409,
            detail="任务已经结束，不能再补充指令；请基于现有结果创建一个新任务。",  # noqa: RUF001
        )
    return await _runtime(request, "POST", f"/agent/runs/{run_id}/inputs", json=await _request_object(request))


@app.post("/api/workspace/runs/{run_id}/approval")
async def approve_workspace_run(request: Request, run_id: str) -> dict[str, Any]:
    """Delegate an owner's approval decision to Runtime's one-time approval inbox."""
    return await _runtime(request, "POST", f"/agent/runs/{run_id}/resume", json=await _request_object(request))


@app.get("/api/workspace/runs/{run_id}/events")
async def workspace_events(request: Request, run_id: str, after_sequence: int = 0) -> StreamingResponse:
    """Proxy an owner/shared-authorized Runtime event stream through the same-origin BFF."""
    # Verify resource access before opening a potentially long-lived upstream connection.
    await _runtime(request, "GET", f"/agent/runs/{run_id}")

    async def stream():
        """Forward bytes unchanged; Runtime remains the session-ledger cursor authority."""
        try:
            async with httpx.AsyncClient(timeout=None, **_mtls_options()) as client, client.stream(
                "GET",
                f"{settings.runtime_base_url.rstrip('/')}/agent/runs/{run_id}/events",
                headers=_identity_headers(request),
                params={"after_sequence": after_sequence},
            ) as response:
                if response.status_code >= 400:
                    return
                async for chunk in response.aiter_bytes():
                    yield chunk
        except httpx.RequestError:
            # The browser sees a normal stream close and can reconnect from its last event ID.
            return

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# The UI is intentionally mounted after API routes: `/api/*` always remains a server-authorized
# projection, while direct browser navigation uses the same-origin static Workspace shell.
static_dir = settings.static_dir
if not static_dir.exists():
    repository_static = Path(__file__).resolve().parents[3] / "agent-web" / "public"
    static_dir = repository_static if repository_static.exists() else static_dir
app.mount("/", StaticFiles(directory=static_dir, html=True), name="agent-web")

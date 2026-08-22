"""Same-origin BFF: browser projections only, never an Agent execution engine."""

from __future__ import annotations

import asyncio
from pathlib import Path
from time import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from platform_infra.identity import OidcIdentityMiddleware
from platform_infra.mtls import mtls_httpx_options
from starlette.middleware.base import BaseHTTPMiddleware

from agent_web_bff.browser_oidc import (
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
        return response


app.add_middleware(BrowserBoundaryMiddleware)
app.add_middleware(
    OidcIdentityMiddleware,
    enabled=settings.oidc_enabled,
    issuer=settings.oidc_issuer,
    audience=settings.oidc_audience,
    jwks_url=settings.oidc_jwks_url,
    public_paths=("/health/ready", "/auth/login", "/auth/callback"),
)
if browser_sessions is not None:
    # Added after the JWT middleware so Starlette executes this outer layer first. It resolves
    # the HttpOnly cookie and injects the token that the inner middleware then validates normally.
    app.add_middleware(BrowserOidcSessionMiddleware, settings=settings, store=browser_sessions)
    app.include_router(build_auth_router(settings, browser_sessions))


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


def _workspace_detail_projection(run: dict[str, Any], *, user_id: str, artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce an owned Runtime result to the business-facing Workspace detail.

    Raw compiled plans, internal traces and tool payloads are intentionally absent here. They
    belong to the reviewer/console projections once their explicit data-domain authorization is
    implemented. The Runtime remains responsible for the initial ownership check.
    """
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
        # Read shares are intentionally not control delegation. Rendering this flag avoids a
        # misleading UI that offers cancel/steering to someone Runtime will correctly reject.
        "can_control": run.get("user_id") == user_id,
        "available_actions": (
            ["approve", "reject", "cancel"]
            if run.get("status") == "WAITING_APPROVAL"
            else ["cancel"]
            if run.get("status") not in {"COMPLETED", "FAILED", "CANCELLED"}
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
        "user_id": request.headers.get("X-User-Id", ""),
        "roles": [item for item in request.headers.get("X-Roles", "").split(",") if item],
        "permissions": [
            item for item in request.headers.get("X-Permissions", "").split(",") if item
        ],
        "authentication": "oidc" if settings.oidc_enabled else "local-development",
        "claim_subject": claims.get("sub", "") if isinstance(claims, dict) else "",
    }


@app.get("/api/workspace/runs")
async def list_workspace_runs(request: Request, limit: int = 30) -> dict[str, Any]:
    """Project only the current user's task list; no target-user filter exists."""
    return await _runtime(request, "GET", "/agent/runs", params={"limit": limit})


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
    payload = await request.json()
    return await _runtime(request, "POST", f"/agent/runs/{run_id}/resume", json=payload)


@app.post("/api/review/runs/{run_id}/transfer", status_code=204)
async def review_transfer(request: Request, run_id: str) -> Response:
    """Transfer only the caller's explicit Review Assignment through Runtime's atomic store operation."""
    payload = await request.json()
    await _runtime(request, "POST", f"/agent/review/runs/{run_id}/transfer", json=payload)
    return Response(status_code=204)


@app.post("/api/review/runs/{run_id}/collaborators", status_code=204)
async def add_review_collaborator(request: Request, run_id: str) -> Response:
    """新增共同审查人而不撤销当前 reviewer；Runtime 验证 Assignment 与细粒度权限。"""
    await _runtime(
        request, "POST", f"/agent/review/runs/{run_id}/collaborators", json=await request.json()
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
        request, "POST", f"/agent/review/runs/{run_id}/comments", json=await request.json()
    )


@app.post("/api/review/runs/{run_id}/feedback")
async def review_feedback(request: Request, run_id: str) -> dict[str, Any]:
    """Write an assigned expert's label to Governance feedback, never into the Run record."""
    _require_permission(request, "run:review:label")
    # Runtime confirms the reviewer-to-Run relation before Governance receives any feedback.
    await _runtime(request, "GET", f"/agent/review/runs/{run_id}")
    payload = await request.json()
    rating = int(payload.get("rating", 0))
    if rating < 1 or rating > 5:
        raise HTTPException(status_code=422, detail="rating must be between 1 and 5")
    headers = _governance_headers(request)
    body = {
        "requestId": run_id,
        "rating": rating,
        "reviewStatus": str(payload.get("review_status", "REVIEWED")),
        "criticality": str(payload.get("criticality", "normal")),
        "expectedAnswer": str(payload.get("expected_answer", ""))[:12_000],
        "tags": [str(item)[:120] for item in payload.get("tags", [])[:20]],
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
    payload = await request.json()
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
async def console_agents(request: Request) -> dict[str, Any]:
    """列出控制面中当前租户的 Agent Draft 摘要，供发布浏览而非运行时解析。"""
    _require_permission(request, "release:read")
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, **_mtls_options()) as client:
            response = await client.get(
                f"{settings.control_plane_base_url.rstrip('/')}/v1/agents",
                headers=_control_plane_headers(request),
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="control plane is unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text[:2_000])
    agents = response.json()
    return {
        "items": [
            {
                "agent_id": item.get("agent_id", ""),
                "revision": item.get("revision", 0),
                "updated_at": item.get("updated_at", ""),
            }
            for item in agents
            if isinstance(item, dict)
        ]
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
    payload = await request.json()
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
    payload = await request.json()
    if str(payload.get("confirm_agent_id", "")) != agent_id:
        raise HTTPException(status_code=422, detail="confirm_agent_id must match the target Agent")
    return await _control_plane(
        request,
        "POST",
        f"/v1/agents/{agent_id}/releases",
        json={
            "version_id": str(payload.get("version_id", "")),
            "environment": str(payload.get("environment", "production")),
            "rollout_percentage": int(payload.get("rollout_percentage", 0)),
            "tenant_allowlist": [str(item) for item in payload.get("tenant_allowlist", [])[:100]],
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
    payload = await request.json()
    if str(payload.get("confirm_release_id", "")) != release_id:
        raise HTTPException(status_code=422, detail="confirm_release_id must match the target Release")
    body = (
        {"rollout_percentage": int(payload.get("rollout_percentage", 100))}
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
    payload = await request.json()
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
    payload = await request.json()
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
            "canaryPercent": int(payload.get("canaryPercent", 5)),
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
    payload = await request.json()
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
    payload = await request.json()
    if str(payload.get("confirmSubject", "")) != subject:
        raise HTTPException(status_code=422, detail="confirmSubject must match subject")
    policy = await _control_plane(request, "GET", "/v1/tenant-policy")
    quotas = dict(policy.get("llm_quotas", {}) or {})
    quotas[subject] = {
        "daily_token_limit": int(payload.get("dailyTokenLimit", 0)),
        "daily_cost_limit_usd": float(payload.get("dailyCostLimitUsd", 0)),
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
    payload = await request.json()
    tenant_id = request.headers.get("X-Tenant-Id", "")
    if str(payload.get("confirmTenantId", "")) != tenant_id:
        raise HTTPException(status_code=422, detail="confirmTenantId must match current tenant")
    return await _governance(request, "POST", "/v1/governance/audit-exports", json={})


@app.post("/api/console/audit-exports/{job_id}/requeue")
async def console_requeue_audit_export(request: Request, job_id: str) -> dict[str, Any]:
    """Explicitly replay one failed export; silent automatic DLQ replay is forbidden."""
    _require_permission(request, "audit:export:requeue")
    _require_high_risk_authentication(request)
    payload = await request.json()
    if str(payload.get("confirmJobId", "")) != job_id:
        raise HTTPException(status_code=422, detail="confirmJobId must match target job")
    return await _governance(
        request, "POST", f"/v1/governance/audit-exports/{job_id}/requeue", json={}
    )


@app.post("/api/workspace/runs")
async def create_workspace_run(request: Request) -> dict[str, Any]:
    """Submit an interactive Workspace task while preserving Runtime's release resolution."""
    payload = await request.json()
    payload["metadata"] = {**payload.get("metadata", {}), "interaction_channel": "agent-web"}
    return await _runtime(request, "POST", "/agent/interactive-runs", json=payload)


@app.get("/api/workspace/runs/{run_id}")
async def workspace_run(request: Request, run_id: str) -> dict[str, Any]:
    """Read one owned Run; Runtime returns 404 for foreign resources."""
    run = await _runtime(request, "GET", f"/agent/runs/{run_id}")
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
    payload = await request.json()
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
    await _runtime(request, "POST", f"/agent/runs/{run_id}/shares", json=await request.json())
    return Response(status_code=204)


@app.post("/api/workspace/runs/{run_id}/inputs", status_code=202)
async def steer_workspace_run(request: Request, run_id: str) -> dict[str, Any]:
    """Submit owner steering through Runtime's persisted mailbox, never via browser-local state."""
    return await _runtime(request, "POST", f"/agent/runs/{run_id}/inputs", json=await request.json())


@app.post("/api/workspace/runs/{run_id}/approval")
async def approve_workspace_run(request: Request, run_id: str) -> dict[str, Any]:
    """Delegate an owner's approval decision to Runtime's one-time approval inbox."""
    return await _runtime(request, "POST", f"/agent/runs/{run_id}/resume", json=await request.json())


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

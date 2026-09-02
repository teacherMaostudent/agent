import asyncio
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from platform_sdk.contracts.capabilities import RuntimeCapability
from platform_sdk.contracts.context import ConversationMessage
from platform_sdk.contracts.desktop_connector import (
    ConnectorGrantRequest,
    ConnectorPairingRequest,
    ConnectorTaskRequest,
)
from platform_sdk.contracts.execution import ExecutionContext
from platform_sdk.contracts.runtime_api import (
    AgentFollowupRequest,
    AgentResumeRequest,
    AgentRunInputRequest,
    AgentRunRequest,
    ReviewAssignmentRequest,
    ReviewCommentRequest,
    ReviewTransferRequest,
    RunShareRequest,
    SessionCompactionRequest,
    SessionForkRequest,
    SkillRunRequest,
    WorkflowResumeRequest,
    WorkflowRunRequest,
)
from platform_sdk.contracts.skills import (
    CompiledSkillPlan,
    OrchestrationOwner,
    SkillBinding,
)
from platform_sdk.contracts.workflow import CompiledWorkflowPlan
from platform_sdk.tools.registry import ToolContext

from agent_runtime_service.runtime.capabilities import CapabilityUnavailable
from agent_runtime_service.runtime.capability_dispatcher import GovernedCapabilityDispatcher
from agent_runtime_service.runtime.capability_handlers import RuntimeCapabilityHandlers
from agent_runtime_service.runtime.connector_artifact_relay import ConnectorArtifactRelay
from agent_runtime_service.runtime.integration import (
    ReleaseNotFoundError,
    ReleaseResolutionError,
    ReleaseResolutionUnavailable,
)
from agent_runtime_service.runtime.mailbox import ClaimedRunMailboxItem, RunMailboxInputType
from agent_runtime_service.runtime.models import (
    ApprovalResume,
    ExecutionLifecycle,
    RuntimeBudget,
    UserInputResume,
)
from agent_runtime_service.runtime.run_state import AgentRunEvent, InvalidRunTransition
from agent_runtime_service.runtime.session_events import RuntimeEventType, model_visible_message
from agent_runtime_service.runtime.skill_runtime import (
    GovernedSkillRuntime,
    InMemorySkillCatalog,
    SkillExecutionRequest,
)
from agent_runtime_service.runtime.snapshot_compiler import CompiledAgentPlan, SnapshotCompileError
from agent_runtime_service.runtime.workflow_runtime import (
    WorkflowExecutionError,
    WorkflowExecutionResult,
    ZeroAgentWorkflowRuntime,
)


def _resolve_release(container, **arguments) -> dict:
    """把跨服务发布解析故障转换为稳定 API 语义，避免已知配置问题冒充 500。"""
    try:
        return container.agent_harness.resolve_release(**arguments)
    except ReleaseNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "agent_release_not_found", "message": str(exc)},
        ) from exc
    except ReleaseResolutionUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "control_plane_unavailable", "message": str(exc)},
        ) from exc
    except ReleaseResolutionError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "release_resolution_rejected", "message": str(exc)},
        ) from exc


def _published_model_routes(snapshot: dict) -> tuple[str, dict[str, dict]]:
    """Return the only model routes a Run may request from its frozen Snapshot.

    A UI selection is deliberately a logical route rather than ``provider:model``.  Credentials,
    base URLs, model revisions and fallback behavior remain Gateway/Control-Plane concerns; a
    caller can choose among published options but can never add a new upstream endpoint.
    """
    spec = snapshot.get("spec") if isinstance(snapshot, dict) else None
    policy = spec.get("model_policy") if isinstance(spec, dict) else None
    if not isinstance(policy, dict):
        raise ValueError("published snapshot has no model policy")
    routes = {
        str(route.get("route_name")): route
        for route in policy.get("routes", [])
        if isinstance(route, dict) and route.get("route_name") and route.get("models")
    }
    default_route = str(policy.get("default_route", ""))
    if default_route not in routes:
        raise ValueError("published snapshot has no executable default model route")
    return default_route, routes


def _plan_for_requested_model_route(
    snapshot: dict, compiled_plan: CompiledAgentPlan, requested_route: str | None
) -> CompiledAgentPlan:
    """Narrow one Run to a Snapshot-declared route while preserving its approved fallback chain."""
    default_route, routes = _published_model_routes(snapshot)
    selected_route = requested_route or default_route
    selected = routes.get(selected_route)
    if selected is None:
        raise ValueError("requested model route is not published for this Agent Release")
    models = [str(item) for item in selected.get("models", []) if str(item)]
    if not models:
        raise ValueError("requested model route has no executable model")
    fallback_models = models[1:]
    fallback = routes.get(str(selected.get("fallback_route", "")))
    if fallback:
        fallback_models.extend(str(item) for item in fallback.get("models", []) if str(item))
    return compiled_plan.model_copy(
        update={
            "logical_model": models[0],
            "fallback_models": list(dict.fromkeys(fallback_models)),
            "data_region": selected.get("data_region"),
        }
    )

router = APIRouter(prefix="/agent", tags=["agent-runtime"])


def _http_internal_false() -> bool:
    """HTTP 请求不能伪造 Worker 内部执行标记；直接 Worker 调用可显式覆盖参数。"""
    return False


def _http_internal_none():
    """把仅供 Worker 直接调用的对象排除出 FastAPI 请求体和 OpenAPI。"""
    return None


def _http_internal_empty() -> str:
    """为内部关联标识提供不可由外部请求扩大的空默认值。"""
    return ""


def _http_agent_owner() -> OrchestrationOwner:
    """外部 Agent API 的编排所有者固定为 Agent；Workflow 嵌套只能由内部覆盖。"""
    return OrchestrationOwner.AGENT


def _public_result(result: dict) -> dict:
    """移除仅用于恢复和内部编排的下划线字段，禁止它们穿透 Runtime API。"""
    return {key: value for key, value in result.items() if not key.startswith("_")}


def _can_read_run(
    store, tenant_id: str, run, user_id: str, permissions: frozenset[str] = frozenset()
) -> bool:
    """判定单个 Run 的读取资格，不能把普通角色误解释成租户范围的数据旁路。

    ``run:tenant:read`` is an explicit, audited administrator-read capability.  It grants only
    observation of Runs in the caller's own tenant; owner-only control actions remain unchanged.
    """
    return (
        "run:tenant:read" in permissions
        or run.user_id == user_id
        or store.is_shared_with(tenant_id, run.run_id, user_id)
    )


def _review_projection(run, assignment: dict[str, str]) -> dict:
    """生成 Review 所需、但不含 Prompt/工具原始输出的受控详情投影。

    审查人需要复核结论、证据标识、冻结快照的结构性计划和预算事实；但原始 Prompt、
    Context 正文、工具参数及工具返回可能含有额外数据域内容，不能因“Review”角色默认
    泄露。后续的证据正文投影必须再经 RAG 数据域授权实现。
    """
    result = run.result if isinstance(run.result, dict) else {}
    evidence = result.get("evidence", [])
    safe_evidence = [
        {
            "evidence_id": str(item.get("evidence_id", item.get("id", item.get("document_id", "")))),
            "source": str(item.get("source", item.get("title", item.get("source_id", "")))),
        }
        for item in evidence
        if isinstance(item, dict)
    ]
    plan_summary: dict[str, object] = {}
    try:
        plan = CompiledAgentPlan.model_validate(result.get("_compiled_plan"))
        plan_summary = {
            "contract_hash": plan.contract_hash,
            "graph": {
                "graph_id": plan.graph_id,
                "entrypoint": plan.graph_entrypoint,
                "terminal_nodes": plan.graph_terminal_nodes,
                "node_count": len(plan.graph_node_kinds),
            },
            "executor_profile": plan.executor_profile,
            "required_capabilities": plan.required_capabilities,
            "logical_model": plan.logical_model,
            "fallback_models": plan.fallback_models,
            "knowledge_bases": [str(item.get("knowledge_base", "")) for item in plan.knowledge],
            "tool_names": [
                str(item.get("tool_name", item.get("name", item.get("tool_id", ""))))
                for item in plan.tools
                if isinstance(item, dict)
            ],
        }
    except Exception:
        # 历史 Run 或部分故障 Run 可能没有成功保存编译计划。明确标记不可用，而非虚构计划。
        plan_summary = {"status": "unavailable"}
    budget = result.get("budget", {})
    return {
        "run_id": run.run_id,
        "agent_id": run.context.agent_id,
        "snapshot_id": run.snapshot_id,
        "status": run.status,
        "runtime_state": run.runtime_state,
        "updated_at": run.updated_at.isoformat(),
        "assignment": assignment,
        "conclusion": {
            "answer": str(result.get("answer", "")),
            "termination_reason": str(result.get("termination_reason", "")),
            "error_code": run.error_code,
        },
        "evidence": safe_evidence,
        "budget": budget if isinstance(budget, dict) else {},
        "plan": plan_summary,
        "approval": {
            "required": run.status == "WAITING_APPROVAL",
            "approval_id": str(result.get("approval_id", "")),
        },
    }


@router.post("/connectors/pairings")
def create_connector_pairing(
    payload: ConnectorPairingRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """创建短时 Desktop 配对码；只登记声明能力，不授予工具权限。"""
    _, _, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    if "connector:pair" not in permissions:
        raise HTTPException(status_code=403, detail="connector:pair permission is required")
    code = secrets.token_urlsafe(24)
    connector_id = request.app.state.container.run_store.create_connector(
        x_tenant_id, x_user_id, payload.device_name, payload.capabilities,
        hashlib.sha256(code.encode()).hexdigest(),
        (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    )
    return {"connector_id": connector_id, "pairing_code": code, "expires_in_seconds": 600}


@router.post("/connectors/{connector_id}/confirm", status_code=status.HTTP_204_NO_CONTENT)
def confirm_connector_pairing(
    connector_id: str,
    request: Request,
    payload: dict,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> Response:
    """确认配对码；确认后仍需每次通过 Runtime/Tool Gateway 做能力授权。"""
    code = str(payload.get("pairing_code", ""))
    if not code or len(code) > 128:
        raise HTTPException(status_code=422, detail="pairing_code is required")
    ok = request.app.state.container.run_store.confirm_connector(
        x_tenant_id, x_user_id, connector_id, hashlib.sha256(code.encode()).hexdigest()
    )
    if not ok:
        raise HTTPException(status_code=404, detail="pairing is invalid or expired")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/connectors/{connector_id}/grants")
def issue_connector_grant(
    connector_id: str,
    payload: ConnectorGrantRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """仅允许已连接设备的 Run 所有者申请单工具一次性执行授权。"""
    _, _, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    if "connector:grant" not in permissions:
        raise HTTPException(status_code=403, detail="connector:grant permission is required")
    if payload.connector_id != connector_id:
        raise HTTPException(status_code=422, detail="connector_id does not match path")
    request.app.state.container.run_store.reconcile_stale_connectors(
        request.app.state.container.settings.connector_heartbeat_timeout_seconds
    )
    connector = request.app.state.container.run_store.get_connector(
        x_tenant_id, x_user_id, connector_id
    )
    if connector is None or connector["status"] != "CONNECTED":
        raise HTTPException(status_code=409, detail="connector is not connected")
    run = request.app.state.container.run_store.get(x_tenant_id, payload.run_id)
    if run is None or run.user_id != x_user_id or run.snapshot_id != payload.snapshot_id:
        raise HTTPException(status_code=404, detail="run or snapshot is not owned by caller")
    if payload.tool_name not in set(connector["capabilities"]):
        raise HTTPException(status_code=403, detail="connector did not declare requested capability")
    compiled_plan = run.result.get("_compiled_plan", {})
    if not isinstance(compiled_plan, dict) or not any(
        item.get("tool_name") == payload.tool_name and item.get("version") == payload.tool_version
        for item in compiled_plan.get("tools", [])
        if isinstance(item, dict)
    ):
        raise HTTPException(status_code=403, detail="tool is not bound by the published run plan")
    tool_gateway = request.app.state.container.capabilities.require(RuntimeCapability.TOOL)
    grant = tool_gateway.issue_connector_grant(
        ToolContext(
            tenant_id=x_tenant_id,
            user_id=x_user_id,
            permissions=frozenset(permissions),
            request_id=f"connector-grant-{uuid4().hex}",
            run_id=payload.run_id,
            snapshot_id=payload.snapshot_id,
        ),
        connector_id,
        payload.run_id,
        payload.snapshot_id,
        payload.tool_name,
        payload.tool_version,
        payload.expires_in_seconds,
    )
    return {"connector_id": connector_id, "run_id": payload.run_id, **grant}


@router.post("/connectors/{connector_id}/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
def heartbeat_connector(
    connector_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> Response:
    """由已确认 Connector 报告存活；状态记录用于断线诊断而不是工具授权。"""
    if not request.app.state.container.run_store.heartbeat_connector(
        x_tenant_id, x_user_id, connector_id
    ):
        raise HTTPException(status_code=409, detail="connector is not connected")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/connectors/{connector_id}/tasks")
def enqueue_connector_task(
    connector_id: str,
    payload: ConnectorTaskRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """把已发布计划中的单个本机动作投递给指定在线 Connector。"""
    _, _, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    if "connector:task:create" not in permissions:
        raise HTTPException(status_code=403, detail="connector:task:create permission is required")
    store = request.app.state.container.run_store
    store.reconcile_stale_connectors(request.app.state.container.settings.connector_heartbeat_timeout_seconds)
    connector = store.get_connector(x_tenant_id, x_user_id, connector_id)
    run = store.get(x_tenant_id, payload.run_id)
    if connector is None or connector["status"] != "CONNECTED":
        raise HTTPException(status_code=409, detail="connector is not connected")
    if run is None or run.user_id != x_user_id or run.snapshot_id != payload.snapshot_id:
        raise HTTPException(status_code=404, detail="run or snapshot is not owned by caller")
    compiled = run.result.get("_compiled_plan", {})
    bound = isinstance(compiled, dict) and any(
        item.get("tool_name") == payload.tool_name and item.get("version") == payload.tool_version
        for item in compiled.get("tools", []) if isinstance(item, dict)
    )
    if payload.tool_name not in set(connector["capabilities"]) or not bound:
        raise HTTPException(status_code=403, detail="connector capability or published tool binding is missing")
    task_id = store.create_connector_task(
        x_tenant_id, x_user_id, connector_id, payload.run_id, payload.snapshot_id,
        payload.tool_name, payload.tool_version, payload.arguments,
        (datetime.now(UTC) + timedelta(seconds=payload.expires_in_seconds)).isoformat(),
    )
    return {"task_id": task_id, "status": "PENDING"}


@router.post("/connectors/{connector_id}/tasks/next")
def claim_connector_task(
    connector_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> dict:
    """由桌面主进程领取一项待执行任务；领取不是执行，也不返回一次性 Grant。"""
    store = request.app.state.container.run_store
    store.reconcile_stale_connectors(request.app.state.container.settings.connector_heartbeat_timeout_seconds)
    connector = store.get_connector(x_tenant_id, x_user_id, connector_id)
    if connector is None or connector["status"] != "CONNECTED":
        raise HTTPException(status_code=409, detail="connector is not connected")
    item = store.claim_connector_task(x_tenant_id, x_user_id, connector_id)
    return {"item": item}


@router.post("/connectors/{connector_id}/tasks/{task_id}/complete", status_code=status.HTTP_204_NO_CONTENT)
def complete_connector_task(
    connector_id: str,
    task_id: str,
    payload: dict,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> Response:
    """接收主进程已脱敏、限长的本机任务结果；拒绝覆盖租约外或终态任务。"""
    result = payload.get("result")
    if not isinstance(result, dict):
        raise HTTPException(status_code=422, detail="result object is required")
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 24_000:
        raise HTTPException(status_code=422, detail="connector result exceeds 24KB limit")
    connector_grant = str(payload.get("connector_grant", ""))
    task = request.app.state.container.run_store.get_connector_task(
        x_tenant_id, x_user_id, connector_id, task_id
    )
    if task is None or task["status"] != "CLAIMED" or not connector_grant:
        raise HTTPException(status_code=409, detail="connector task or one-time grant is unavailable")
    _, _, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    try:
        request.app.state.container.capabilities.require(RuntimeCapability.TOOL).record_connector_result(
            ToolContext(
                tenant_id=x_tenant_id, user_id=x_user_id, permissions=frozenset(permissions),
                request_id=f"connector-result-{task_id}", run_id=str(task["run_id"]),
                snapshot_id=str(task["snapshot_id"]), connector_id=connector_id,
                connector_grant=connector_grant,
            ),
            str(task["tool_name"]), str(task["tool_version"]), task_id,
            hashlib.sha256(encoded.encode()).hexdigest(),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="connector result audit was rejected") from exc
    # 将已审计的脱敏摘要交给 Context 工件边界；交付失败不能回滚已经完成的本机动作。
    run = request.app.state.container.run_store.get(x_tenant_id, str(task["run_id"]))
    artifact_delivery_status = "NOT_REQUIRED"
    artifact_id = ""
    if run is not None:
        try:
            artifact = _capability(
                request.app.state.container, RuntimeCapability.CONTEXT
            ).create_text_artifact(
                run.context.root_task_id or run.run_id,
                json.dumps(result, ensure_ascii=False, indent=2),
                tenant_id=x_tenant_id,
                user_id=x_user_id,
            )
            result["artifact_id"] = artifact.artifact_id
            artifact_id = str(artifact.artifact_id)
            artifact_delivery_status = "DELIVERED"
            if str(task["tool_name"]) == "controlled_scan":
                request.app.state.container.run_store.register_artifact_ingestion(
                    x_tenant_id,
                    x_user_id,
                    str(task["run_id"]),
                    run.context.root_task_id or run.run_id,
                    task_id,
                    artifact_id,
                )
        except Exception as exc:
            result["artifact_delivery_status"] = "PENDING"
            artifact_delivery_status = "PENDING"
            request.app.state.container.run_store.enqueue_connector_artifact(
                x_tenant_id,
                x_user_id,
                task_id,
                run.context.root_task_id or run.run_id,
                result,
                type(exc).__name__,
            )
    ok = request.app.state.container.run_store.complete_connector_task(
        x_tenant_id, x_user_id, connector_id, task_id, result,
        hashlib.sha256(encoded.encode()).hexdigest(),
        artifact_delivery_status=artifact_delivery_status,
        artifact_id=artifact_id,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="connector task is not claimable")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/connectors/artifact-outbox/relay")
def relay_connector_artifact_outbox(
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict[str, int]:
    """受限 Relay 入口，供 Worker/运维任务重放 Connector Artifact 交付。"""
    _, _, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    if "connector:artifact:relay" not in permissions:
        raise HTTPException(status_code=403, detail="connector:artifact:relay permission is required")
    # A browser/admin call can only replay its verified tenant. The independent workload is the
    # only code path allowed to scan globally, using its dedicated database/network identity.
    return ConnectorArtifactRelay(request.app.state.container).run_once(tenant_id=x_tenant_id)


@router.get("/connectors/artifact-outbox/dead-letters")
def list_connector_artifact_dead_letters(
    request: Request,
    limit: int = 50,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict[str, list[dict]]:
    """返回当前租户的死信元数据，不暴露 Connector 结果正文。"""
    _, _, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    if "connector:artifact:dlq:read" not in permissions:
        raise HTTPException(status_code=403, detail="connector:artifact:dlq:read permission is required")
    return {
        "items": request.app.state.container.run_store.list_connector_artifact_dead_letters(
            x_tenant_id, limit=limit
        )
    }


@router.post("/connectors/artifact-outbox/dead-letters/{outbox_id}/requeue")
def requeue_connector_artifact_dead_letter(
    outbox_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict[str, str]:
    """以显式高风险权限重放单条死信，幂等地拒绝非死信或跨租户记录。"""
    _, _, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    if "connector:artifact:dlq:requeue" not in permissions:
        raise HTTPException(
            status_code=403, detail="connector:artifact:dlq:requeue permission is required"
        )
    if not request.app.state.container.run_store.requeue_connector_artifact_dead_letter(
        x_tenant_id, outbox_id
    ):
        raise HTTPException(status_code=404, detail="connector artifact dead letter not found")
    container = request.app.state.container
    container.run_store.enqueue_governance(
        {
            "event_id": f"evt_{uuid4().hex}",
            "source_service": "agent-runtime",
            "event_type": "connector.artifact.dead_letter_requeued",
            "trace_id": "",
            "tenant_id": x_tenant_id,
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": {"outbox_id": outbox_id, "requeued_by": x_user_id},
        }
    )
    container.governance.flush()
    return {"outbox_id": outbox_id, "status": "RETRY"}


@router.get("/connectors/{connector_id}/tasks/{task_id}")
def get_connector_task_status(
    connector_id: str,
    task_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> dict:
    """供桌面端确认结果是否已审计并交付；响应不回显结果正文或一次性 Grant。"""
    item = request.app.state.container.run_store.get_connector_task(
        x_tenant_id, x_user_id, connector_id, task_id
    )
    if item is None:
        raise HTTPException(status_code=404, detail="connector task not found")
    return item


@router.delete("/connectors/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_connector(
    connector_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> Response:
    """撤销当前用户的 Desktop Connector，撤销后不可重新确认。"""
    _, _, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    if "connector:revoke" not in permissions:
        raise HTTPException(status_code=403, detail="connector:revoke permission is required")
    if not request.app.state.container.run_store.revoke_connector(x_tenant_id, x_user_id, connector_id):
        raise HTTPException(status_code=404, detail="connector not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/connectors/{connector_id}")
def get_connector(
    connector_id: str, request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> dict:
    """读取已配对设备状态，不返回配对码、Token 或本机路径。"""
    request.app.state.container.run_store.reconcile_stale_connectors(
        request.app.state.container.settings.connector_heartbeat_timeout_seconds
    )
    item = request.app.state.container.run_store.get_connector(x_tenant_id, x_user_id, connector_id)
    if item is None:
        raise HTTPException(status_code=404, detail="connector not found")
    return item


@router.get("/capabilities")
def runtime_capabilities(request: Request) -> dict:
    """返回不含业务数据的执行器能力声明，供 Control Plane 发布前做实例证明。"""
    container = request.app.state.container
    return {
        "service": "agent-runtime",
        "catalog_version": container.settings.executor_catalog_version,
        "capability_catalog_version": container.capabilities.version,
        "capability_contract_version": "runtime-capability-contract/v1",
        "capability_manifest_digest": container.capabilities.manifest_digest,
        "executor_profiles": list(container.agent_harness.executor_profiles),
        "execution_providers": [
            {
                "profile": provider.profile,
                "mode": provider.mode.value,
                "lifecycle": provider.lifecycle.value,
                "reasoning": provider.reasoning.value,
                "supports_resume": provider.supports_resume,
            }
            for provider in container.executor_catalog.providers
        ],
        "capabilities": list(container.capabilities.names),
        "capability_manifests": [
            item.model_dump(mode="json") for item in container.capabilities.manifests
        ],
    }


@router.post("/skills/run")
def run_skill(
    payload: SkillRunRequest,
    request: Request,
    x_tenant_id: str = Header(alias="X-Tenant-Id"),
    x_user_id: str = Header(default="agent-user", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
    x_request_id: str = Header(default="", alias="X-Request-Id"),
    x_trace_id: str = Header(default="", alias="X-Trace-Id"),
) -> dict:
    """解析并执行 Active SkillVersion；调用方不能提交完整计划。"""
    container = request.app.state.container
    tenant_id, user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    if container.control_plane is None:
        raise HTTPException(status_code=503, detail="Control Plane is required for Skill execution")
    trace_id = x_trace_id or f"trace_{uuid4().hex}"
    resolution = container.control_plane.resolve_skill(
        tenant_id, payload.skill_id, payload.version, trace_id
    )
    plan = CompiledSkillPlan.model_validate(resolution.get("plan"))
    if resolution.get("artifact_digest") != payload.artifact_digest:
        raise HTTPException(status_code=409, detail="Skill artifact digest drift")
    binding = SkillBinding(
        skill_id=payload.skill_id,
        version=payload.version,
        artifact_digest=payload.artifact_digest,
        max_budget_fraction=1.0,
    )
    skill_execution_id = f"skill_{uuid4().hex}"
    context = ExecutionContext.create(
        request_id=x_request_id or f"req_{uuid4().hex}",
        trace_id=trace_id,
        session_id="",
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id="",
        agent_version="",
        snapshot_id=payload.artifact_digest,
        deadline_seconds=payload.deadline_seconds,
        attempt_budget=20,
        orchestration_owner=OrchestrationOwner.WORKFLOW,
        workflow_id="direct-skill-invocation",
        skill_execution_id=skill_execution_id,
    )
    budget = RuntimeBudget(
        deadline_at=context.deadline_at,
        max_steps=1,
        max_llm_calls=1,
        max_tool_calls=len(plan.tools),
        max_retrieval_rounds=len(plan.knowledge),
        max_cost_usd=payload.max_cost_usd,
    )
    result = GovernedSkillRuntime(InMemorySkillCatalog([plan]), container.skill_executor).execute(
        SkillExecutionRequest(
            invocation_id=skill_execution_id,
            binding=binding,
            capability_id=payload.capability_id,
            input=payload.input,
            context=context,
            caller_permissions=frozenset(permissions),
            agent_permissions=frozenset(permissions),
            plan_id=f"direct-skill-plan:{skill_execution_id}",
            plan_admission_id=f"direct-skill-admission:{payload.artifact_digest}",
            step_id=skill_execution_id,
        ),
        budget,
    )
    return result.model_dump(mode="json")


@router.post("/workflows/run")
def run_workflow(
    payload: WorkflowRunRequest,
    request: Request,
    x_tenant_id: str = Header(alias="X-Tenant-Id"),
    x_user_id: str = Header(default="workflow-user", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
    x_request_id: str = Header(default="", alias="X-Request-Id"),
    x_trace_id: str = Header(default="", alias="X-Trace-Id"),
    _temporal_worker_execution: Annotated[bool, Depends(_http_internal_false)] = False,
    _release_resolution: Annotated[dict | None, Depends(_http_internal_none)] = None,
    _run_id: Annotated[str, Depends(_http_internal_empty)] = "",
    _checkpoint: Annotated[dict | None, Depends(_http_internal_none)] = None,
    _signal: Annotated[dict | None, Depends(_http_internal_none)] = None,
) -> dict:
    """执行 Active 零 Agent Workflow；不创建 Agent Session 或调用 Planner。"""
    container = request.app.state.container
    tenant_id, user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    if container.control_plane is None and _release_resolution is None:
        raise HTTPException(
            status_code=503, detail="Control Plane is required for Workflow execution"
        )
    trace_id = x_trace_id or f"trace_{uuid4().hex}"
    resolution = _release_resolution or container.control_plane.resolve_workflow(
        tenant_id, payload.workflow_id, payload.environment, trace_id
    )
    plan = CompiledWorkflowPlan.model_validate(resolution.get("plan"))
    digest = str(resolution.get("artifact_digest", ""))
    if plan.durable and not _temporal_worker_execution:
        queue = container.async_runs
        if queue is None or not hasattr(queue, "submit_workflow"):
            raise HTTPException(
                status_code=503,
                detail="Durable Workflow requires a Temporal workflow queue",
            )
        return queue.submit_workflow(
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "permissions": ",".join(sorted(permissions)),
                "request_id": x_request_id or f"req_{uuid4().hex}",
                "trace_id": trace_id,
                "payload": payload.model_dump(mode="json"),
                "release_resolution": resolution,
            }
        )
    context = ExecutionContext.create(
        request_id=x_request_id or f"req_{uuid4().hex}",
        trace_id=trace_id,
        session_id="",
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id="",
        agent_version="",
        snapshot_id=digest,
        deadline_seconds=payload.deadline_seconds,
        attempt_budget=sum(item.max_attempts for item in plan.steps),
        orchestration_owner=OrchestrationOwner.WORKFLOW,
        workflow_id=plan.workflow_id,
        run_id=_run_id or None,
    )
    budget = RuntimeBudget(
        deadline_at=context.deadline_at,
        max_steps=len(plan.steps),
        max_llm_calls=len(plan.steps),
        max_tool_calls=len(plan.steps),
        max_retrieval_rounds=len(plan.steps),
        max_cost_usd=payload.max_cost_usd,
        max_attempts=context.attempt_budget_remaining,
    )
    handlers = RuntimeCapabilityHandlers(
        container,
        permissions=frozenset(permissions),
        budget=budget,
        agent_runner=lambda provider, step_payload, execution_context: (
            container._run_agent_capability(
                provider,
                step_payload,
                execution_context,
                permissions=frozenset(permissions),
                budget=budget,
            )
        ),
    )
    dispatcher = GovernedCapabilityDispatcher(
        plan.capability_providers,
        plan.capability_routing,
        handlers.handlers(),
    )
    try:
        result = ZeroAgentWorkflowRuntime(dispatcher.dispatch_output).run(
            plan,
            payload.input,
            context,
            checkpoint=(
                WorkflowExecutionResult.model_validate(_checkpoint) if _checkpoint else None
            ),
            signal=_signal,
            # Durable 路径每次 Activity 只推进一步，让 Temporal History
            # 成为步骤边界的恢复事实；同步短流程仍可一次执行完。
            max_steps=1 if _temporal_worker_execution else None,
        )
    except (WorkflowExecutionError, RuntimeError, ValueError) as exc:
        if _temporal_worker_execution:
            # 保留原异常类型，使 Temporal 的 non_retryable_error_types 能
            # 区分业务失败与 Worker 崩溃，避免 HTTPException 造成整步重试。
            raise
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        **result.model_dump(mode="json"),
        "run_id": context.run_id,
        "root_task_id": context.root_task_id,
        "artifact_digest": digest,
        "orchestration_owner": context.orchestration_owner.value,
        "topology": "none",
    }


@router.post("/workflows/runs/{run_id}/resume")
def resume_workflow(
    run_id: str,
    payload: WorkflowResumeRequest,
    request: Request,
    x_tenant_id: str = Header(alias="X-Tenant-Id"),
    x_user_id: str = Header(default="workflow-user", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """经 OIDC 重新验证后向 Temporal Workflow 发送一次恢复信号。"""
    tenant_id, _, _ = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    queue = request.app.state.container.async_runs
    if queue is None or not hasattr(queue, "resume_workflow"):
        raise HTTPException(status_code=503, detail="Temporal workflow queue is unavailable")
    result = queue.resume_workflow(tenant_id, run_id, payload.signal)
    if result is None:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    return result


@router.get("/workflows/runs/{run_id}")
def get_workflow_run(
    run_id: str,
    request: Request,
    x_tenant_id: str = Header(alias="X-Tenant-Id"),
    x_user_id: str = Header(default="workflow-user", alias="X-User-Id"),
) -> dict:
    """按租户查询 Temporal 中的零 Agent Workflow 运行。"""
    tenant_id, _, _ = _trusted_identity(request, x_tenant_id, x_user_id, "")
    queue = request.app.state.container.async_runs
    if queue is None or not hasattr(queue, "get_workflow"):
        raise HTTPException(status_code=503, detail="Temporal workflow queue is unavailable")
    result = queue.get_workflow(tenant_id, run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    return result


@router.post("/workflows/runs/{run_id}/cancel")
def cancel_workflow_run(
    run_id: str,
    request: Request,
    x_tenant_id: str = Header(alias="X-Tenant-Id"),
    x_user_id: str = Header(default="workflow-user", alias="X-User-Id"),
) -> dict:
    """通过 Temporal 取消整个 Workflow RootTask，不仅中断当前 HTTP。"""
    tenant_id, _, _ = _trusted_identity(request, x_tenant_id, x_user_id, "")
    queue = request.app.state.container.async_runs
    if queue is None or not hasattr(queue, "cancel_workflow"):
        raise HTTPException(status_code=503, detail="Temporal workflow queue is unavailable")
    result = queue.cancel_workflow(tenant_id, run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    return result


def _capability(container, capability: RuntimeCapability):
    """把未部署能力转为 503，避免接口因内部属性缺失出现不透明错误。"""
    try:
        return container.capability(capability)
    except CapabilityUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _is_durable_plan(plan: CompiledAgentPlan) -> bool:
    """以编译后的生命周期而非历史 Profile 名称判断是否必须走 Temporal。"""
    return plan.execution_requirements.lifecycle == ExecutionLifecycle.DURABLE_WORKFLOW


def _effective_attempt_budget(payload: AgentRunRequest, runtime_limits: dict, settings) -> int:
    """计算 Run 的总下游尝试额度，并保证默认值不会与已发布子额度自相矛盾。

    显式 ``attempt_budget`` 仍可主动缩小运行；未显式提供时，总额度至少覆盖快照允许的
    LLM、工具和检索调用之和。此前固定默认值 6 小于桌面 Release 的 4+3+3，导致各项
    子预算尚未耗尽时提前失败。
    """
    if payload.attempt_budget is not None:
        return payload.attempt_budget
    max_llm_calls = int(runtime_limits.get("max_llm_calls", settings.agent_max_llm_calls))
    max_tool_calls = int(runtime_limits.get("max_tool_calls", settings.agent_max_tool_calls))
    max_retrieval_rounds = int(
        runtime_limits.get("max_retrieval_rounds", settings.agent_max_retrieval_rounds)
    )
    published_action_budget = min(
        1000,
        max_llm_calls + max_tool_calls + max_retrieval_rounds,
    )
    return max(settings.agent_attempt_budget, published_action_budget)


def _trusted_identity(
    request: Request,
    tenant_id: str,
    user_id: str,
    permissions_header: str,
) -> tuple[str, str, set[str]]:
    """读取经 OIDC 中间件重建的身份，并禁止生产 API 信任裸 Header。

    OIDC 启用时 Middleware 已覆盖请求 Header；此处再要求验证声明存在，防止某个
    路由被错误挂到中间件之外。关闭 OIDC 仅是本地开发兼容路径，不能作为部署身份根。
    """
    # FastAPI resolves ``Header`` defaults before production calls reach this helper.  Unit tests
    # invoke endpoint functions directly, where an omitted optional Header remains a descriptor;
    # normalize it to the same empty-permission meaning instead of weakening authorization.
    permissions_value = permissions_header if isinstance(permissions_header, str) else ""
    settings = request.app.state.container.settings
    claims = request.scope.get("auth.claims")
    if settings.oidc_enabled and not isinstance(claims, dict):
        raise HTTPException(status_code=401, detail="verified OIDC identity is required")
    if settings.oidc_enabled:
        # OIDC middleware has already deleted caller values and rebuilt these
        # headers from configurable claim mappings. Reading the rebuilt values
        # keeps Runtime compatible with enterprise-specific tenant/user claims.
            permissions = {item.strip() for item in permissions_value.split(",") if item.strip()}
    else:
        permissions = {item.strip() for item in permissions_value.split(",") if item.strip()}
    if not tenant_id or not user_id:
        raise HTTPException(status_code=401, detail="trusted tenant and user identity are required")
    return tenant_id, user_id, permissions


def _trusted_subject_roles(request: Request, roles_header: str) -> frozenset[str]:
    """返回 OIDC 中间件重建的角色，仅作为发布灰度的收窄信号。

    角色不会被用于工具授权，也不会让请求获得新的 Snapshot 能力。生产 OIDC 开启时，
    中间件已经移除来路 Header 并从已验签 JWT 重建该字段；本地模式仅用于联调。
    """
    value = roles_header if isinstance(roles_header, str) else ""
    if request.app.state.container.settings.oidc_enabled and not isinstance(
        request.scope.get("auth.claims"), dict
    ):
        raise HTTPException(status_code=401, detail="verified OIDC identity is required")
    return frozenset(item.strip() for item in value.split(",") if item.strip())


@router.get("/model-routes")
def list_published_model_routes(
    request: Request,
    agent_id: str,
    environment: str = "production",
    session_id: str = "",
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="rag:read", alias="X-Permissions"),
) -> dict:
    """List model choices from the exact Release that this task session will execute.

    Resolving with the same generated ``session_id`` pins the release before the task form is
    submitted.  The later POST reuses that binding, so a canary transition cannot make the
    dropdown describe one Snapshot while the Run executes another.
    """
    tenant_id, user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    if "rag:read" not in permissions:
        raise HTTPException(status_code=403, detail="rag:read permission is required")
    if not session_id.strip():
        raise HTTPException(status_code=422, detail="session_id is required to pin model choices")
    container = request.app.state.container
    resolution = _resolve_release(
        container,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        environment=environment,
        session_id=session_id,
        trace_id=f"model-catalog:{session_id}",
    )
    snapshot = dict(resolution.get("snapshot") or {})
    try:
        default_route, routes = _published_model_routes(snapshot)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "agent_id": agent_id,
        "environment": environment,
        "session_id": session_id,
        "release_id": str(resolution.get("release_id", "")),
        "snapshot_id": str(resolution.get("version_id", "")),
        "default_route": default_route,
        "items": [
            {
                "route_name": name,
                "models": [str(item) for item in route.get("models", [])],
                "data_region": route.get("data_region"),
                "fallback_route": route.get("fallback_route"),
            }
            for name, route in routes.items()
        ],
    }


@router.post("/run")
def run_agent(
    payload: AgentRunRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="rag:read", alias="X-Permissions"),
    x_roles: str = Header(default="", alias="X-Roles"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    x_trace_id: str | None = Header(default=None, alias="X-Trace-Id"),
    x_run_id: str | None = Header(default=None, alias="X-Run-Id"),
    _temporal_worker_execution: Annotated[bool, Depends(_http_internal_false)] = False,
    _release_resolution: Annotated[dict | None, Depends(_http_internal_none)] = None,
    _orchestration_owner: Annotated[
        OrchestrationOwner, Depends(_http_agent_owner)
    ] = OrchestrationOwner.AGENT,
    _workflow_id: Annotated[str, Depends(_http_internal_empty)] = "",
) -> dict:
    """同步执行一个受发布快照约束的 Agent Run。

    调用方可缩小但不能扩大快照的步骤、时间、成本与工具输出限制。先持久化 Run，再
    写入用户消息并执行；终态与治理事件在同一事务入 Outbox，避免结果成功而审计丢失。
    """
    started = monotonic()
    container = request.app.state.container
    if not container.settings.agent_enabled:
        raise HTTPException(status_code=503, detail="agent graph is disabled")
    x_tenant_id, x_user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    subject_roles = _trusted_subject_roles(request, x_roles)
    if "rag:read" not in permissions:
        raise HTTPException(status_code=403, detail="rag:read permission is required")
    request_id = x_request_id or f"agent-{uuid4().hex}"
    session_id = payload.session_id or request_id
    trace_id = x_trace_id or request_id
    try:
        # Harness 仅协调发布解析和快照加载; API 不再直接调用 Control Plane 或编译快照。
        resolution = _release_resolution or _resolve_release(
            container,
            tenant_id=x_tenant_id,
            user_id=x_user_id,
            agent_id=payload.agent_id,
            environment=payload.environment,
            session_id=session_id,
            trace_id=trace_id,
            subject_roles=subject_roles,
        )
        loaded_snapshot = container.agent_harness.load_snapshot(
            resolution,
            tenant_id=x_tenant_id,
            agent_id=payload.agent_id,
        )
    except (SnapshotCompileError, RuntimeError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "snapshot_not_executable", "message": str(exc)},
        ) from exc
    snapshot = loaded_snapshot.snapshot
    try:
        compiled_plan = _plan_for_requested_model_route(
            loaded_snapshot.snapshot, loaded_snapshot.plan, payload.model_route
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if _is_durable_plan(compiled_plan) and not _temporal_worker_execution:
        raise HTTPException(
            status_code=409,
            detail="durable-workflow releases must be submitted through POST /runs",
        )
    runtime_limits = snapshot.get("spec", {}).get("runtime_limits", {})
    configured_steps = payload.max_steps or container.settings.agent_max_steps
    max_steps = min(configured_steps, int(runtime_limits.get("max_steps", configured_steps)))
    configured_deadline = payload.deadline_seconds or container.settings.agent_deadline_seconds
    deadline_seconds = min(
        configured_deadline,
        int(runtime_limits.get("max_execution_seconds", configured_deadline)),
    )
    execution = container.agent_harness.create_execution_context(
        request_id=request_id,
        trace_id=trace_id,
        session_id=session_id,
        tenant_id=x_tenant_id,
        user_id=x_user_id,
        agent_id=payload.agent_id,
        loaded_snapshot=loaded_snapshot,
        release_resolution=resolution,
        deadline_seconds=deadline_seconds,
        attempt_budget=_effective_attempt_budget(payload, runtime_limits, container.settings),
        run_id=x_run_id,
        parent_run_id=str(payload.metadata.get("_parent_run_id", "")),
        parent_session_id=str(payload.metadata.get("_parent_session_id", "")),
        root_task_id=str(payload.metadata.get("_root_task_id", "")),
        collaboration_snapshot_id=str(payload.metadata.get("_collaboration_snapshot_id", "")),
        business_operation_id=str(payload.metadata.get("_business_operation_id", "")),
        orchestration_owner=_orchestration_owner,
        workflow_id=_workflow_id,
    )
    # Run 是一次执行尝试；Turn 是该 Session 内的一次用户交互。稳定派生 ID 使
    # Temporal Activity 重放不会凭空创建第二个 Turn。
    turn_id = f"turn_{execution.run_id}"
    attempt_id = f"attempt_{execution.run_id}"
    # Temporal Activity 重试会携带同一 run_id; 已有终态/待审批 Run 必须只返回既有结果,
    # 不能再次写入用户消息或重新触发 Graph。运行中的 Run 由 Temporal Activity 重放恢复。
    existing_before_create = container.run_store.get(x_tenant_id, execution.run_id)
    run_already_started = existing_before_create is not None
    if existing_before_create and existing_before_create.status != "RUNNING":
        return {
            **_public_result(existing_before_create.result),
            "run_id": existing_before_create.run_id,
            "snapshot_id": existing_before_create.snapshot_id,
            "status": existing_before_create.status,
            "idempotent_replay": True,
        }
    created, started_event = container.run_store.create_with_session_event(execution)
    if created.run_id != execution.run_id:
        return {
            **_public_result(created.result),
            "run_id": created.run_id,
            "snapshot_id": created.snapshot_id,
            "status": created.status,
            "idempotent_replay": True,
        }
    existing = container.run_store.get(x_tenant_id, execution.run_id)
    if existing and existing.cancel_requested:
        raise HTTPException(status_code=409, detail="run was cancelled before execution")
    if started_event is not None:
        # 运行记录已在 Store 提交后才发布事件；Event Bus 不得成为状态机的写入前置条件。
        container.publish_session_event(started_event)
        turn_event = container.run_store.append_session_event(
            execution,
            RuntimeEventType.TURN_STARTED,
            metadata={"origin": "runtime-api"},
            turn_id=turn_id,
            attempt_id=attempt_id,
        )
        container.publish_session_event(turn_event)
    # 进程可能在 ``create`` 提交后、START 事件提交前崩溃。重放同一 run_id 时仅允许
    # 从 CREATED 补齐这一步；其他状态绝不被请求重试重新初始化。
    if started_event is not None or (existing is not None and existing.runtime_state == "CREATED"):
        try:
            _, state_event = container.run_store.transition_state(
                x_tenant_id,
                execution.run_id,
                AgentRunEvent.START,
                metadata={"source": "runtime-api"},
            )
        except InvalidRunTransition as exc:
            raise HTTPException(
                status_code=409, detail={"code": "invalid_run_state", "message": str(exc)}
            ) from exc
        if state_event is not None:
            container.publish_session_event(state_event)
    if run_already_started:
        try:
            container.reconcile_tool_intents(execution)
        except RuntimeError as exc:
            # 对不确定副作用 fail-closed；Temporal Activity 会按既有策略稍后恢复，
            # 同步调用方得到明确的“等待对账”而不是偷偷重复调用业务系统。
            raise HTTPException(
                status_code=409, detail={"code": "tool_recovery_pending", "message": str(exc)}
            ) from exc
    try:
        if not run_already_started:
            user_event = container.run_store.append_session_event(
                execution,
                RuntimeEventType.USER_MESSAGE,
                metadata={"message_source": "runtime-api"},
                model_message=model_visible_message(
                    "user", payload.task, source="runtime-api.user-message"
                ),
                turn_id=turn_id,
                attempt_id=attempt_id,
            )
            container.publish_session_event(user_event)
            _capability(container, RuntimeCapability.CONTEXT).append_message(
                session_id,
                ConversationMessage(role="user", content=payload.task, metadata=payload.metadata),
                x_tenant_id,
                x_user_id,
            )
        max_cost = min(
            payload.max_cost_usd or container.settings.agent_max_cost_usd,
            float(runtime_limits.get("max_cost_usd", container.settings.agent_max_cost_usd)),
        )
        budget = RuntimeBudget(
            deadline_at=execution.deadline_at,
            max_steps=max_steps,
            max_llm_calls=int(
                runtime_limits.get("max_llm_calls", container.settings.agent_max_llm_calls)
            ),
            max_tool_calls=int(
                runtime_limits.get("max_tool_calls", container.settings.agent_max_tool_calls)
            ),
            max_retrieval_rounds=int(
                runtime_limits.get(
                    "max_retrieval_rounds",
                    container.settings.agent_max_retrieval_rounds,
                )
            ),
            max_cost_usd=max_cost,
            max_attempts=execution.attempt_budget_remaining,
        )
        # 只有根运行创建不可变总账；子 Agent 在 Graph 中对同一 root_task_id
        # 申请操作级预留，不能以自己的缩小预算覆盖根任务上限。
        if not execution.parent_run_id:
            container.run_store.initialize_root_budget(
                x_tenant_id,
                execution.root_task_id,
                max_cost_usd=budget.max_cost_usd,
                max_steps=budget.max_steps,
            )
        # A caller may narrow, but never expand, the deployment's untrusted
        # tool-output limit before results are included in a decision prompt.
        configured_tool_limit = container.settings.agent_tool_result_max_chars
        request_tool_limit = payload.metadata.get("tool_result_max_chars")
        if isinstance(request_tool_limit, int):
            configured_tool_limit = min(configured_tool_limit, max(1, request_tool_limit))
        runtime_metadata = {
            **payload.metadata,
            "tool_result_max_chars": configured_tool_limit,
            "runtime_environment": payload.environment,
            # Root 运行在此固定协作组合; 子 Agent 只继承引用和缩小后的额度。
            "_root_task_id": execution.root_task_id or execution.run_id,
            "_collaboration_snapshot_id": execution.collaboration_snapshot_id,
            "_business_operation_id": execution.business_operation_id,
        }
        result = container.agent_harness.run(
            {
                "task": payload.task,
                "document_id": payload.document_id,
                "content": payload.content,
                "metadata": runtime_metadata,
                "tenant_id": x_tenant_id,
                "user_id": x_user_id,
                "permissions": sorted(permissions),
                "request_id": request_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "attempt_id": attempt_id,
                "run_id": execution.run_id,
                "root_task_id": execution.root_task_id or execution.run_id,
                "collaboration_snapshot_id": execution.collaboration_snapshot_id,
                "business_operation_id": execution.business_operation_id,
                "orchestration_owner": execution.orchestration_owner.value,
                "workflow_id": execution.workflow_id,
                "trace_id": execution.trace_id,
                "agent_id": execution.agent_id,
                "agent_version": execution.agent_version,
                "snapshot_id": execution.snapshot_id,
                "release_id": execution.release_id,
                "release_stage": execution.release_stage,
                "release_projection_revision": execution.release_projection_revision,
                "traffic_policy_version": execution.traffic_policy_version,
                "side_effect_policy_version": execution.side_effect_policy_version,
                "agent_snapshot": snapshot,
                "compiled_plan": compiled_plan.model_dump(mode="json"),
                "executor_profile": compiled_plan.executor_profile,
                "graph_version": execution.graph_version,
                "flow_version": container.settings.runtime_flow_version,
                "deadline_at": execution.deadline_at.isoformat(),
                "attempt_budget_remaining": execution.attempt_budget_remaining,
                "budget": budget.model_dump(mode="json"),
                "step_count": 0,
                "max_steps": max_steps,
                "observations": [],
                "evidence": [],
                "execution_trace": [],
                "subagent_invocations": {},
                "agent_results": [],
                "temporal_worker_execution": _temporal_worker_execution,
            },
            execution.run_id,
            compiled_plan,
        )
        if result.status != "WAITING_APPROVAL":
            _capability(container, RuntimeCapability.CONTEXT).append_message(
                session_id,
                ConversationMessage(
                    role="assistant",
                    content=result.answer,
                    metadata={
                        "request_id": request_id,
                        "termination_reason": result.termination_reason,
                    },
                ),
                x_tenant_id,
                x_user_id,
            )
            assistant_event = container.run_store.append_session_event(
                execution,
                RuntimeEventType.ASSISTANT_MESSAGE,
                status=result.status,
                metadata={"termination_reason": result.termination_reason},
                model_message=model_visible_message(
                    "assistant", result.answer, source="runtime-api.assistant-message"
                ),
                turn_id=turn_id,
                attempt_id=attempt_id,
            )
            container.publish_session_event(assistant_event)
            completed_turn = container.run_store.append_session_event(
                execution,
                RuntimeEventType.TURN_COMPLETED,
                status=result.status,
                metadata={"termination_reason": result.termination_reason},
                turn_id=turn_id,
                attempt_id=attempt_id,
            )
            container.publish_session_event(completed_turn)
    except Exception as exc:
        event = container.governance.event_for_run(
            execution,
            "FAILED",
            {},
            type(exc).__name__,
        )
        session_event = container.run_store.finish_and_enqueue(
            execution.run_id,
            "FAILED",
            {},
            event,
            type(exc).__name__,
        )
        container.governance.flush()
        if session_event is not None:
            container.publish_session_event(session_event)
        interrupted = container.run_store.append_session_event(
            execution,
            RuntimeEventType.TURN_INTERRUPTED,
            status="FAILED",
            metadata={"reason": type(exc).__name__},
            turn_id=turn_id,
            attempt_id=attempt_id,
        )
        container.publish_session_event(interrupted)
        session_interrupted = container.run_store.append_session_event(
            execution,
            RuntimeEventType.SESSION_INTERRUPTED,
            status="FAILED",
            metadata={"reason": type(exc).__name__, "run_id": execution.run_id},
            turn_id=turn_id,
            attempt_id=attempt_id,
        )
        container.publish_session_event(session_interrupted)
        raise
    body = {
        **result.model_dump(mode="json"),
        "run_id": execution.run_id,
        "snapshot_id": execution.snapshot_id,
        "latency_ms": int((monotonic() - started) * 1_000),
    }
    event = container.governance.event_for_run(execution, result.status, body)
    # 编译计划仅供审批恢复选择同一执行器, 不能进入对外结果或治理事件载荷。
    persisted_result = {
        **body,
        "_compiled_plan": compiled_plan.model_dump(mode="json"),
        "_runtime_environment": payload.environment,
    }
    # 最终回答的对象存储交付是可选增强：S3/Context 不可用时任务结果和状态机仍必须
    # 正确完成。失败事实被保存为明确状态，而不是给 Workspace 伪造一个可下载 Artifact。
    if result.status == "COMPLETED" and result.answer.strip():
        try:
            artifact = _capability(container, RuntimeCapability.CONTEXT).create_text_artifact(
                execution.root_task_id or execution.run_id,
                result.answer,
                tenant_id=x_tenant_id,
                user_id=x_user_id,
            )
            persisted_result["artifact_ids"] = [artifact.artifact_id]
        except Exception as exc:  # Best-effort delivery must not roll back an already valid answer.
            persisted_result["artifact_delivery_status"] = f"unavailable:{type(exc).__name__}"
    session_event = container.run_store.finish_and_enqueue(
        execution.run_id, result.status, persisted_result, event
    )
    container.governance.flush()
    if session_event is not None:
        container.publish_session_event(session_event)
    return body


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
def submit_agent_run(
    payload: AgentRunRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="rag:read", alias="X-Permissions"),
    x_roles: str = Header(default="", alias="X-Roles"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    x_trace_id: str | None = Header(default=None, alias="X-Trace-Id"),
) -> dict:
    """把运行提交给持久化异步队列并返回 202；同租户 request_id 重试保持幂等。"""
    x_tenant_id, x_user_id, trusted_permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    subject_roles = _trusted_subject_roles(request, x_roles)
    if "rag:read" not in trusted_permissions:
        raise HTTPException(status_code=403, detail="rag:read permission is required")
    request_id = x_request_id or f"agent-{uuid4().hex}"
    # Durable Profile 必须在提交时就被识别, 防止同步 API 在 Worker 外绕开 Temporal。
    container = request.app.state.container
    resolution = _resolve_release(
        container,
        tenant_id=x_tenant_id,
        user_id=x_user_id,
        agent_id=payload.agent_id,
        environment=payload.environment,
        session_id=payload.session_id or request_id,
        trace_id=x_trace_id or request_id,
        subject_roles=subject_roles,
    )
    loaded = container.agent_harness.load_snapshot(
        resolution, tenant_id=x_tenant_id, agent_id=payload.agent_id
    )
    if not _is_durable_plan(loaded.plan):
        raise HTTPException(
            status_code=409,
            detail="asynchronous /runs is reserved for durable-workflow releases",
        )
    submitted = _capability(container, RuntimeCapability.WORKFLOW).submit(
        {
            "payload": payload.model_dump(mode="json"),
            "tenant_id": x_tenant_id,
            "user_id": x_user_id,
            "permissions": ",".join(sorted(trusted_permissions)),
            "subject_roles": sorted(subject_roles),
            "request_id": request_id,
            "trace_id": x_trace_id or request_id,
            "data_region": loaded.plan.data_region,
            # 提交时冻结 Resolve 结果; Worker 绝不能在 Release 切换后重新选择版本。
            "release_resolution": resolution,
        }
    )
    return submitted


@router.post("/interactive-runs", status_code=status.HTTP_202_ACCEPTED)
def submit_interactive_agent_run(
    payload: AgentRunRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="rag:read", alias="X-Permissions"),
    x_roles: str = Header(default="", alias="X-Roles"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    x_trace_id: str | None = Header(default=None, alias="X-Trace-Id"),
) -> dict:
    """为桌面等交互客户端提交可观察运行，并立即返回稳定 Run ID。

    与只接受 Durable Profile 的 ``/runs`` 不同，此入口允许短任务进入 Runtime 已部署
    的持久队列。发布解析仍在提交时冻结，Worker 仍复用唯一 ``run_agent`` 状态机；
    因而桌面端可以安全订阅事件、取消或恢复，而无需复制一套执行逻辑。
    """
    tenant_id, user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    subject_roles = _trusted_subject_roles(request, x_roles)
    if "rag:read" not in permissions:
        raise HTTPException(status_code=403, detail="rag:read permission is required")
    container = request.app.state.container
    request_id = x_request_id or f"interactive-{uuid4().hex}"
    trace_id = x_trace_id or request_id
    resolution = _resolve_release(
        container,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=payload.agent_id,
        environment=payload.environment,
        session_id=payload.session_id or request_id,
        trace_id=trace_id,
        subject_roles=subject_roles,
    )
    try:
        loaded = container.agent_harness.load_snapshot(
            resolution, tenant_id=tenant_id, agent_id=payload.agent_id
        )
    except (SnapshotCompileError, RuntimeError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "snapshot_not_executable", "message": str(exc)},
        ) from exc
    submitted = _capability(container, RuntimeCapability.WORKFLOW).submit(
        {
            "payload": payload.model_dump(mode="json"),
            "tenant_id": tenant_id,
            "user_id": user_id,
            "permissions": ",".join(sorted(permissions)),
            "subject_roles": sorted(subject_roles),
            "request_id": request_id,
            "trace_id": trace_id,
            "data_region": loaded.plan.data_region,
            "release_resolution": resolution,
            "interaction_channel": "desktop",
        }
    )
    # Shadow is a best-effort, separately persisted replay.  The primary run already has a
    # stable response here, so a disabled mirror, an absent candidate, or a later mirror failure
    # cannot delay or change the user's task.  The mirror worker resolves a SHADOW-only Release
    # itself and never reuses the primary session binding.
    submit_shadow = getattr(container, "submit_shadow_mirror", None)
    shadow = submit_shadow(
        {
            "run_id": f"shadow_{uuid4().hex}",
            "source_run_id": submitted.get("run_id", ""),
            "payload": payload.model_dump(mode="json"),
            "tenant_id": tenant_id,
            "user_id": "shadow-worker",
            "permissions": ",".join(sorted(permissions)),
            "subject_roles": sorted(subject_roles),
            "request_id": f"shadow:{request_id}",
            "trace_id": trace_id,
            "data_region": loaded.plan.data_region,
        }
    ) if callable(submit_shadow) else None
    if shadow:
        submitted["shadow_mirror"] = {
            "run_id": shadow.get("run_id", ""),
            "status": shadow.get("status", "QUEUED"),
        }
    return submitted


@router.post("/runs/{run_id}/resume")
def resume_run(
    run_id: str,
    payload: AgentResumeRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    _temporal_worker_execution: Annotated[bool, Depends(_http_internal_false)] = False,
    _user_input: Annotated[UserInputResume | None, Depends(_http_internal_none)] = None,
    _claimed_control: Annotated[
        ClaimedRunMailboxItem | None, Depends(_http_internal_none)
    ] = None,
) -> dict:
    """恢复等待审批的检查点。

    只允许原租户且状态为 WAITING_APPROVAL 的运行恢复；批准/拒绝都会生成新的治理
    事件并在终态时写回会话记忆。
    """
    x_tenant_id, x_user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    container = request.app.state.container
    run = container.run_store.get(x_tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    # HTTP 调用者默认只能恢复自己的 Run。唯一例外是已被显式指派的审查人处理审批；
    # 它仍需要两项权限，且不适用于 Steering/UserInput。Temporal Worker 以内部依赖注入
    # 调用时才可跨用户处理已绑定的持久化检查点，该标记不能由 HTTP 请求传入。
    reviewer_approval = False
    if not _temporal_worker_execution and run.user_id != x_user_id:
        reviewer_approval = (
            _user_input is None
            and "agent:review" in permissions
            and "run:review:approve" in permissions
            and container.run_store.is_assigned_reviewer(x_tenant_id, run_id, x_user_id)
        )
        if not reviewer_approval:
            raise HTTPException(status_code=404, detail="run not found")
    expected_status = "WAITING_INPUT" if _user_input is not None else "WAITING_APPROVAL"
    if run.status != expected_status:
        raise HTTPException(
            status_code=409, detail=f"run is not waiting for {expected_status.lower()}"
        )
    if run.cancel_requested:
        raise HTTPException(status_code=409, detail="run was cancelled")
    # Judge 策略的候选选择只能由 Governance 工作负载提交；Runtime 不同步调用
    # Judge 模型，以免把治理平面重新耦合进线上执行环。
    pending_conflict = next(
        (
            item
            for item in run.result.get("interrupts", [])
            if isinstance(item, dict) and item.get("type") == "subagent_conflict"
        ),
        None,
    )
    if (
        not _temporal_worker_execution
        and pending_conflict is not None
        and pending_conflict.get("strategy") == "judge"
        and "governance:judge:resolve" not in permissions
    ):
        raise HTTPException(status_code=403, detail="governance judge permission is required")
    claimed_control = _claimed_control
    if _user_input is None and claimed_control is None:
        # 审批决定也首先作为受版本化的 Inbox 控制输入落账。邮箱仅保存结构化决定，
        # 不保存用户自由文本；稳定 approval_id 让 API/Temporal 重试共用同一条消息。
        approval_key = payload.approval_id or x_request_id or f"approval-{uuid4().hex}"
        try:
            message_id = container.run_store.enqueue_mailbox_input(
                x_tenant_id,
                run_id,
                RunMailboxInputType.APPROVAL_RESULT,
                idempotency_key=approval_key,
                control_input={
                    "approved": payload.approved,
                    "approval_id": payload.approval_id,
                    "reason": payload.reason,
                    "decided_by": x_user_id,
                    "selected_provider_agent_id": payload.selected_provider_agent_id,
                    "payload_version": "approval-result/v1",
                },
            )
            claimed_control = container.run_store.claim_mailbox_input(x_tenant_id, run_id)
        except (LookupError, InvalidRunTransition, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if claimed_control is None or claimed_control.message_id != message_id:
            raise HTTPException(
                status_code=409, detail="approval inbox input is unavailable for resume"
            )
    effective_payload = (
        AgentResumeRequest.model_validate(claimed_control.control_input)
        if claimed_control is not None
        else payload
    )
    budget = run.result.get("budget", {})
    max_steps = int(budget.get("max_steps", container.settings.agent_max_steps))
    try:
        compiled_plan = CompiledAgentPlan.model_validate(run.result.get("_compiled_plan"))
    except Exception as exc:
        raise HTTPException(
            status_code=409, detail="persisted execution plan is unavailable"
        ) from exc
    if _is_durable_plan(compiled_plan) and not _temporal_worker_execution:
        queue = _capability(container, RuntimeCapability.WORKFLOW)
        if not hasattr(queue, "resume"):
            raise HTTPException(status_code=503, detail="Temporal durable executor is unavailable")
        signal = (
            {
                "_user_input": _user_input.model_dump(mode="json"),
                "decided_by": x_user_id,
            }
            if _user_input is not None
            else {
                "approved": effective_payload.approved,
                "approval_id": effective_payload.approval_id,
                "reason": effective_payload.reason,
                "decided_by": x_user_id,
                "selected_provider_agent_id": effective_payload.selected_provider_agent_id,
            }
        )
        if claimed_control is not None:
            signal["_claimed_control"] = {
                "message_id": claimed_control.message_id,
                "input_type": claimed_control.input_type.value,
                "lease_token": claimed_control.lease_token,
                "control_input": claimed_control.control_input,
            }
        queued = queue.resume(
            x_tenant_id,
            run_id,
            signal,
        )
        if queued is None:
            raise HTTPException(status_code=409, detail="durable workflow is no longer available")
        return queued
    # 只有实际执行恢复的进程才能推进状态机；外层 Durable API 仅投递 Signal，
    # 否则 Worker 会看到已经 RUNNING 的 Run 并拒绝自身恢复。
    input_metadata = (
        {
            "mailbox_message_id": _user_input.message_id,
            "input_type": RunMailboxInputType.STEERING.value,
        }
        if _user_input is not None
        else {
            "mailbox_message_id": claimed_control.message_id if claimed_control else "",
            "input_type": RunMailboxInputType.APPROVAL_RESULT.value,
            "approval_id": effective_payload.approval_id,
            "approved": effective_payload.approved,
        }
    )
    received_event = container.run_store.append_session_event(
        run.context,
        RuntimeEventType.RUN_INPUT_RECEIVED,
        status=run.status,
        metadata=input_metadata,
        turn_id=f"turn_{run_id}",
        attempt_id=f"attempt_{run_id}",
    )
    container.publish_session_event(received_event)
    try:
        _, state_event = container.run_store.transition_state(
            x_tenant_id,
            run_id,
            AgentRunEvent.STEERING_RECEIVED
            if _user_input is not None
            else AgentRunEvent.APPROVAL_RECEIVED,
            metadata=input_metadata,
        )
    except InvalidRunTransition as exc:
        raise HTTPException(
            status_code=409, detail={"code": "invalid_run_state", "message": str(exc)}
        ) from exc
    if state_event is not None:
        container.publish_session_event(state_event)
    result = container.agent_harness.resume(
        run_id,
        _user_input
        or ApprovalResume(
            approved=effective_payload.approved,
            approval_id=effective_payload.approval_id,
            decided_by=x_user_id,
            reason=effective_payload.reason,
            selected_provider_agent_id=effective_payload.selected_provider_agent_id,
        ),
        max_steps=max_steps,
        plan=compiled_plan,
    )
    body = {
        **result.model_dump(mode="json"),
        "run_id": run_id,
        "snapshot_id": run.snapshot_id,
        "latency_ms": int((datetime.now(UTC) - run.created_at).total_seconds() * 1_000),
    }
    turn_id = f"turn_{run_id}"
    attempt_id = f"attempt_{run_id}"
    if result.status != "WAITING_APPROVAL":
        _capability(container, RuntimeCapability.CONTEXT).append_message(
            run.context.session_id,
            ConversationMessage(
                role="assistant",
                content=result.answer,
                metadata={
                    "request_id": run.context.request_id,
                    "termination_reason": result.termination_reason,
                },
            ),
            run.tenant_id,
            run.user_id,
        )
        assistant_event = container.run_store.append_session_event(
            run.context,
            RuntimeEventType.ASSISTANT_MESSAGE,
            status=result.status,
            metadata={"termination_reason": result.termination_reason, "resumed": True},
            model_message=model_visible_message(
                "assistant", result.answer, source="runtime-api.assistant-message"
            ),
            turn_id=turn_id,
            attempt_id=attempt_id,
        )
        container.publish_session_event(assistant_event)
        turn_event = container.run_store.append_session_event(
            run.context,
            RuntimeEventType.TURN_COMPLETED,
            status=result.status,
            metadata={"termination_reason": result.termination_reason, "resumed": True},
            turn_id=turn_id,
            attempt_id=attempt_id,
        )
        container.publish_session_event(turn_event)
    event = container.governance.event_for_run(run.context, result.status, body)
    persisted_result = {
        **body,
        "_compiled_plan": compiled_plan.model_dump(mode="json"),
        "_runtime_environment": run.result.get("_runtime_environment", "production"),
    }
    session_event = container.run_store.finish_and_enqueue(
        run_id, result.status, persisted_result, event
    )
    container.governance.flush()
    if session_event is not None:
        container.publish_session_event(session_event)
    if claimed_control is not None and not container.run_store.acknowledge_mailbox_input(
        claimed_control.message_id, claimed_control.lease_token
    ):
        raise HTTPException(
            status_code=409, detail="approval inbox lease was lost before acknowledgement"
        )
    return body


@router.post("/runs/{run_id}/followups")
def followup_subagent_run(
    run_id: str,
    payload: AgentFollowupRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    x_trace_id: str | None = Header(default=None, alias="X-Trace-Id"),
) -> dict:
    """以谱系授权启动子 Agent 的下一轮任务，实现跨进程的冷继续而非任意互调。

    新运行继承子会话、父运行关联及已绑定 Release；它不会重用旧检查点或把新输入写入
    另一个 Agent 的内存。等待审批的子运行仍只能走原有 ``resume`` 流程。
    """
    x_tenant_id, x_user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    if "agent:subagent:control" not in permissions:
        raise HTTPException(status_code=403, detail="agent:subagent:control permission is required")
    container = request.app.state.container
    target = container.run_store.get(x_tenant_id, run_id)
    parent = container.run_store.get(x_tenant_id, payload.parent_run_id)
    if target is None or parent is None:
        raise HTTPException(status_code=404, detail="subagent run or parent run not found")
    if target.status == "RUNNING" or target.status == "WAITING_APPROVAL":
        raise HTTPException(status_code=409, detail="active subagent must be resumed or cancelled")
    if parent.user_id != x_user_id or target.user_id != x_user_id:
        raise HTTPException(status_code=403, detail="subagent lineage belongs to a different user")
    if not container.run_store.is_run_ancestor(x_tenant_id, payload.parent_run_id, run_id):
        raise HTTPException(status_code=403, detail="parent run is not an ancestor of the subagent")
    continuation = AgentRunRequest(
        task=payload.task,
        agent_id=target.agent_id,
        environment=str(target.result.get("_runtime_environment", "production")),
        session_id=target.context.session_id,
        metadata={
            "_parent_run_id": run_id,
            "_continuation_of": run_id,
            "_lineage_root_run_id": payload.parent_run_id,
        },
        max_steps=payload.max_steps,
        max_cost_usd=payload.max_cost_usd,
    )
    try:
        target_plan = CompiledAgentPlan.model_validate(target.result.get("_compiled_plan"))
    except Exception:
        target_plan = None
    if target_plan is not None and _is_durable_plan(target_plan):
        return submit_agent_run(
            continuation,
            request,
            x_tenant_id=x_tenant_id,
            x_user_id=x_user_id,
            x_permissions=",".join(sorted(permissions)),
            x_request_id=x_request_id or f"followup-{uuid4().hex}",
            x_trace_id=x_trace_id or target.context.trace_id,
        )
    return run_agent(
        continuation,
        request,
        x_tenant_id=x_tenant_id,
        x_user_id=x_user_id,
        x_permissions=",".join(sorted(permissions)),
        x_request_id=x_request_id or f"followup-{uuid4().hex}",
        x_trace_id=x_trace_id or target.context.trace_id,
    )


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """读取租户范围内的运行或异步队列状态，不返回其他租户的检查点/提交内容。"""
    x_tenant_id, x_user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    run = request.app.state.container.run_store.get(x_tenant_id, run_id)
    if run is not None and not _can_read_run(
        request.app.state.container.run_store, x_tenant_id, run, x_user_id, permissions
    ):
        raise HTTPException(status_code=404, detail="run not found")
    if run is None:
        queued = _capability(request.app.state.container, RuntimeCapability.WORKFLOW).get(
            x_tenant_id, run_id
        )
        if queued is None:
            raise HTTPException(status_code=404, detail="run not found")
        return queued
    body = run.model_dump(mode="json")
    body["result"] = _public_result(body.get("result") or {})
    return body


@router.get("/runs")
def list_my_runs(
    request: Request,
    limit: int = 30,
    offset: int = 0,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """Return owned/shared runs, or a tenant-admin observation projection when explicitly allowed.

    Workspace 只能基于该端点构建“我的任务”。Review 的团队队列必须等待显式的
    assignment/share 数据模型和单独 permission 后再提供，不能通过放宽 owner 条件实现。
    """
    tenant_id, user_id, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    # Limit is bounded at the API boundary as well as in the repository so a future storage
    # implementation cannot accidentally turn one Workspace request into a tenant-wide scan.
    bounded_limit = min(max(limit, 1), 100)
    bounded_offset = min(max(offset, 0), 10_000)
    tenant_wide = "run:tenant:read" in permissions
    store = request.app.state.container.run_store
    # Count and list use the same owner/shared predicate.  Never infer the total from a short page:
    # doing so would make the final page look like the complete task history.
    if tenant_wide:
        total_items = store.count_for_tenant(tenant_id)
        runs = store.list_for_tenant(tenant_id, limit=bounded_limit, offset=bounded_offset)
    else:
        total_items = store.count_for_user(tenant_id, user_id)
        runs = store.list_for_user(tenant_id, user_id, limit=bounded_limit, offset=bounded_offset)
    return {
        "scope": "tenant-admin" if tenant_wide else "owned-or-shared",
        "offset": bounded_offset,
        "limit": bounded_limit,
        "total_items": total_items,
        "items": [
            {
                "run_id": run.run_id,
                "agent_id": run.context.agent_id,
                "snapshot_id": run.context.snapshot_id,
                "status": run.status,
                "runtime_state": run.runtime_state,
                "created_at": run.created_at.isoformat(),
                "updated_at": run.updated_at.isoformat(),
                "error_code": run.error_code,
                # 列表页不能成为计划、Prompt 或工具返回的批量导出接口。完整执行依据
                # 只在单 Run 详情的所有权校验后按需读取，Review 另行采用授权投影。
                "summary": {
                    "answer": str(run.result.get("answer", ""))[:1_000],
                    "termination_reason": str(run.result.get("termination_reason", "")),
                    "evidence_count": len(run.result.get("evidence", [])),
                    "tool_call_count": len(run.result.get("observations", [])),
                    "waiting_for_approval": run.status == "WAITING_APPROVAL",
                },
            }
            for run in runs
        ]
    }


@router.post("/runs/{run_id}/shares", status_code=status.HTTP_204_NO_CONTENT)
def share_run(
    run_id: str, payload: RunShareRequest, request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> Response:
    """Owner 以最小只读权限共享单一 Run；共享人不能继承取消或审批能力。"""
    tenant_id, user_id, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    if "run:share" not in permissions:
        raise HTTPException(status_code=403, detail="run:share permission is required")
    store = request.app.state.container.run_store
    run = store.get(tenant_id, run_id)
    if run is None or run.user_id != user_id:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        store.share_run(tenant_id, run_id, payload.user_id, user_id, payload.reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/runs/{run_id}/artifacts")
def list_run_artifacts(
    run_id: str,
    request: Request,
    limit: int = 100,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """投影已授权 Run 的 Artifact 索引，绝不把 content_ref 或对象存储位置交给浏览器。

    Artifact 的原文和下载授权属于其数据域；Workspace 只获得可审计的类型、哈希和
    创建时间。共享读者可以查看相同索引，但不能由此推导对象存储路径或凭据。
    """
    tenant_id, user_id, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    container = request.app.state.container
    run = container.run_store.get(tenant_id, run_id)
    if run is None or not _can_read_run(container.run_store, tenant_id, run, user_id, permissions):
        raise HTTPException(status_code=404, detail="run not found")
    artifacts = container.runtime_context.context.list_task_artifacts(
        run.context.root_task_id or run.run_id,
        tenant_id=tenant_id,
        limit=min(max(limit, 1), 100),
    )
    return {
        "items": [
            {
                "artifact_id": artifact.artifact_id,
                "artifact_type": artifact.artifact_type,
                "logical_name": artifact.logical_name or artifact.artifact_type,
                "version": artifact.version,
                "previous_artifact_id": artifact.previous_artifact_id,
                "media_type": artifact.media_type,
                "content_sha256": artifact.content_sha256,
                "created_at": artifact.created_at.isoformat(),
            }
            for artifact in artifacts
        ]
    }


def _record_artifact_read(
    container,
    run,
    *,
    user_id: str,
    event_type: RuntimeEventType,
    governance_type: str,
    metadata: dict[str, object],
) -> None:
    """追加内容访问证据，但不把预览正文或对象存储位置写入 Runtime 审计事件。"""
    access_event = container.run_store.append_session_event(
        run.context,
        event_type,
        status=run.status,
        metadata={**metadata, "authorized_user_id": user_id},
    )
    container.publish_session_event(access_event)
    container.run_store.enqueue_governance(
        {
            "event_id": f"gov_{access_event.event_id}",
            "source_service": "agent-runtime",
            "event_type": governance_type,
            "trace_id": run.context.trace_id,
            "tenant_id": run.context.tenant_id,
            "occurred_at": access_event.occurred_at.isoformat(),
            "payload": {
                "run_id": run.run_id,
                "authorized_user_id": user_id,
                "session_event_id": access_event.event_id,
                **metadata,
            },
        }
    )
    container.governance.flush()


@router.get("/runs/{run_id}/artifact-ingestions")
def list_run_artifact_ingestions(
    run_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """向所有者、分配审查者或显式租户观察员返回 Artifact 摄取与晋升状态。"""
    tenant_id, user_id, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    store = request.app.state.container.run_store
    run = store.get(tenant_id, run_id)
    if run is None or not (
        "run:tenant:read" in permissions
        or run.user_id == user_id
        or store.is_assigned_reviewer(tenant_id, run_id, user_id)
    ):
        raise HTTPException(status_code=404, detail="run not found")
    items = store.list_artifact_ingestions(tenant_id, run_id)
    # Runtime owns approval/submission; ingestion owns parsing/indexing. This bounded live
    # projection avoids copying the downstream state into a second database.
    ingestion = request.app.state.container.ingestion
    for item in items:
        job_id = str(item.get("ingestion_job_id") or "")
        if not job_id:
            continue
        try:
            job = ingestion.get_job(job_id, tenant_id=tenant_id, user_id=user_id)
            item["downstream_status"] = str(job.get("status") or "UNKNOWN")
            item["downstream_error"] = str(job.get("error") or "")[:1_000]
        except Exception:
            # Approval history remains readable during an ingestion outage; unavailable is
            # explicit and is never rewritten as a successful index operation.
            item["downstream_status"] = "UNAVAILABLE"
            item["downstream_error"] = "ingestion status is temporarily unavailable"
    return {"items": items}


@router.post("/runs/{run_id}/artifact-ingestions/{artifact_id}/decision")
def decide_run_artifact_ingestion(
    run_id: str,
    artifact_id: str,
    payload: dict,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """在桌面扫描内容进入 RAG 前消费一次显式审批决定，并固定审批理由。"""
    tenant_id, user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    if "rag:ingest:approve" not in permissions:
        raise HTTPException(status_code=403, detail="rag:ingest:approve permission is required")
    if str(payload.get("confirm_artifact_id", "")) != artifact_id:
        raise HTTPException(status_code=422, detail="confirm_artifact_id must match artifact_id")
    store = request.app.state.container.run_store
    run = store.get(tenant_id, run_id)
    if run is None or not (
        run.user_id == user_id or store.is_assigned_reviewer(tenant_id, run_id, user_id)
    ):
        raise HTTPException(status_code=404, detail="run not found")
    approved = bool(payload.get("approved"))
    reason = str(payload.get("reason", "")).strip()
    if not reason:
        raise HTTPException(status_code=422, detail="approval reason is required")
    decision = store.decide_artifact_ingestion(
        tenant_id,
        run_id,
        artifact_id,
        user_id,
        approved=approved,
        reason=reason,
    )
    if decision is None:
        raise HTTPException(status_code=409, detail="artifact approval is unavailable or consumed")
    session_event = store.append_session_event(
        run.context,
        RuntimeEventType.ARTIFACT_INGESTION_DECIDED,
        status=run.status,
        metadata={
            "artifact_id": artifact_id,
            "approved": approved,
            "approved_by": user_id,
            "request_id": decision["request_id"],
        },
    )
    request.app.state.container.publish_session_event(session_event)
    store.enqueue_governance(
        {
            "event_id": f"gov_{session_event.event_id}",
            "source_service": "agent-runtime",
            "event_type": "artifact.ingestion.decided",
            "trace_id": run.context.trace_id,
            "tenant_id": tenant_id,
            "occurred_at": session_event.occurred_at.isoformat(),
            "payload": {
                "run_id": run_id,
                "artifact_id": artifact_id,
                "approved": approved,
                "approved_by": user_id,
                "request_id": decision["request_id"],
            },
        }
    )
    request.app.state.container.governance.flush()
    return decision


@router.get("/runs/{run_id}/artifacts/{artifact_id}/preview")
def get_run_artifact_preview(
    run_id: str,
    artifact_id: str,
    request: Request,
    max_chars: int = 50_000,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """经 Run 所有者/共享授权后返回受限文本预览，并记录每次内容访问审计。"""
    tenant_id, user_id, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    container = request.app.state.container
    run = container.run_store.get(tenant_id, run_id)
    if run is None or not _can_read_run(container.run_store, tenant_id, run, user_id, permissions):
        raise HTTPException(status_code=404, detail="run not found")
    try:
        preview = container.runtime_context.context.artifact_preview(
            run.context.root_task_id or run.run_id,
            artifact_id,
            tenant_id=tenant_id,
            max_chars=min(max(max_chars, 256), 100_000),
        )
    except httpx.HTTPStatusError as exc:
        mapped = 404 if exc.response.status_code == 404 else exc.response.status_code
        raise HTTPException(status_code=mapped, detail="artifact preview is unavailable") from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="artifact preview is unavailable") from exc
    _record_artifact_read(
        container,
        run,
        user_id=user_id,
        event_type=RuntimeEventType.ARTIFACT_PREVIEWED,
        governance_type="artifact.previewed",
        metadata={"artifact_id": artifact_id, "version": preview.version},
    )
    return preview.model_dump(mode="json")


@router.get("/runs/{run_id}/artifacts/{artifact_id}/compare/{base_artifact_id}")
def compare_run_artifacts(
    run_id: str,
    artifact_id: str,
    base_artifact_id: str,
    request: Request,
    max_chars: int = 80_000,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """比较同一 Run 的不可变工件版本，不向浏览器泄露底层对象存储引用。"""
    tenant_id, user_id, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    container = request.app.state.container
    run = container.run_store.get(tenant_id, run_id)
    if run is None or not _can_read_run(container.run_store, tenant_id, run, user_id, permissions):
        raise HTTPException(status_code=404, detail="run not found")
    try:
        comparison = container.runtime_context.context.compare_artifacts(
            run.context.root_task_id or run.run_id,
            artifact_id,
            base_artifact_id,
            tenant_id=tenant_id,
            max_chars=min(max(max_chars, 1_000), 100_000),
        )
    except httpx.HTTPStatusError as exc:
        mapped = 404 if exc.response.status_code == 404 else exc.response.status_code
        raise HTTPException(status_code=mapped, detail="artifact comparison is unavailable") from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="artifact comparison is unavailable") from exc
    _record_artifact_read(
        container,
        run,
        user_id=user_id,
        event_type=RuntimeEventType.ARTIFACT_COMPARED,
        governance_type="artifact.compared",
        metadata={
            "artifact_id": artifact_id,
            "base_artifact_id": base_artifact_id,
            "target_version": comparison.target_version,
            "base_version": comparison.base_version,
        },
    )
    return comparison.model_dump(mode="json")


@router.get("/runs/{run_id}/artifacts/{artifact_id}/download")
def get_run_artifact_download(
    run_id: str,
    artifact_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict[str, str | int | bool]:
    """签发短期下载授权并记录不含 URL/对象键的访问事实。"""
    tenant_id, user_id, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    container = request.app.state.container
    run = container.run_store.get(tenant_id, run_id)
    if run is None or not _can_read_run(container.run_store, tenant_id, run, user_id, permissions):
        raise HTTPException(status_code=404, detail="run not found")
    try:
        context_client = container.runtime_context.context
        if hasattr(context_client, "artifact_download_authorization"):
            authorization = context_client.artifact_download_authorization(
                run.context.root_task_id or run.run_id, artifact_id, tenant_id=tenant_id
            )
        else:
            authorization = {
                "url": context_client.artifact_download_url(
                    run.context.root_task_id or run.run_id, artifact_id, tenant_id=tenant_id
                ),
                "expires_in_seconds": 300,
                "supports_range": True,
            }
    except httpx.HTTPStatusError as exc:
        # Context has already constrained the Artifact to the same RootTask. Preserve a 404 for
        # a guessed Artifact ID, but never relay storage topology or presigned URL details.
        status_code = 404 if exc.response.status_code == 404 else 409
        raise HTTPException(status_code=status_code, detail="artifact is not downloadable") from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="artifact delivery is unavailable") from exc
    access_event = container.run_store.append_session_event(
        run.context,
        RuntimeEventType.ARTIFACT_DOWNLOAD_AUTHORIZED,
        status=run.status,
        metadata={
            "artifact_id": artifact_id,
            "authorized_user_id": user_id,
            "expires_in_seconds": int(authorization.get("expires_in_seconds", 300)),
            "supports_range": bool(authorization.get("supports_range", True)),
        },
    )
    container.publish_session_event(access_event)
    container.run_store.enqueue_governance(
        {
            "event_id": f"gov_{access_event.event_id}",
            "source_service": "agent-runtime",
            "event_type": "artifact.download.authorized",
            "trace_id": run.context.trace_id,
            "tenant_id": tenant_id,
            "occurred_at": access_event.occurred_at.isoformat(),
            "payload": {
                "run_id": run_id,
                "artifact_id": artifact_id,
                "authorized_user_id": user_id,
                "session_event_id": access_event.event_id,
                "expires_in_seconds": int(authorization.get("expires_in_seconds", 300)),
                "supports_range": bool(authorization.get("supports_range", True)),
            },
        }
    )
    container.governance.flush()
    return {
        "url": str(authorization["url"]),
        "expires_in_seconds": int(authorization.get("expires_in_seconds", 300)),
        "supports_range": bool(authorization.get("supports_range", True)),
    }


@router.post("/runs/{run_id}/review-assignments", status_code=status.HTTP_204_NO_CONTENT)
def assign_run_reviewer(
    run_id: str,
    payload: ReviewAssignmentRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> Response:
    """创建审查任务指派；只有明确具备指派权限的主体可扩大他人的可见范围。"""
    tenant_id, user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    if "run:review:assign" not in permissions:
        raise HTTPException(status_code=403, detail="run:review:assign permission is required")
    store = request.app.state.container.run_store
    try:
        # Current Runtime stores make Assignment and Governance Outbox insertion atomic.  The
        # compatibility fallback preserves narrow unit-test doubles and never runs in deployment.
        assign_with_audit = getattr(store, "assign_reviewer_and_audit", None)
        if callable(assign_with_audit):
            assign_with_audit(tenant_id, run_id, payload.reviewer_id, user_id, payload.reason)
        else:
            store.assign_reviewer(tenant_id, run_id, payload.reviewer_id, user_id, payload.reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/review/runs/{run_id}/transfer", status_code=status.HTTP_204_NO_CONTENT)
def transfer_review_run(
    run_id: str, payload: ReviewTransferRequest, request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> Response:
    """由当前被指派审查人转交队列项；转交不会让其保留隐式读取权。"""
    tenant_id, user_id, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    if "agent:review" not in permissions or "run:review:transfer" not in permissions:
        raise HTTPException(status_code=403, detail="review transfer permission is required")
    try:
        request.app.state.container.run_store.transfer_reviewer(
            tenant_id, run_id, user_id, payload.reviewer_id, payload.reason
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/review/runs/{run_id}/collaborators", status_code=status.HTTP_204_NO_CONTENT)
def add_review_collaborator(
    run_id: str,
    payload: ReviewAssignmentRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> Response:
    """由已指派且具备协作授权的审查人新增共同 reviewer，不移除原 Assignment。"""
    tenant_id, user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    if "agent:review" not in permissions or "run:review:assign" not in permissions:
        raise HTTPException(status_code=403, detail="review collaborator permission is required")
    store = request.app.state.container.run_store
    if not store.is_assigned_reviewer(tenant_id, run_id, user_id):
        raise HTTPException(status_code=404, detail="run not found")
    try:
        store.assign_reviewer(tenant_id, run_id, payload.reviewer_id, user_id, payload.reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/review/runs")
def list_review_runs(
    request: Request,
    limit: int = 30,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """返回当前 reviewer 的显式队列，不以角色名扩大到同租户其他运行。"""
    tenant_id, user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    if "agent:review" not in permissions:
        raise HTTPException(status_code=403, detail="agent:review permission is required")
    assignments = request.app.state.container.run_store.list_for_reviewer(
        tenant_id, user_id, limit=min(max(limit, 1), 100)
    )
    return {
        "items": [
            {
                "run_id": run.run_id,
                "agent_id": run.context.agent_id,
                "status": run.status,
                "updated_at": run.updated_at.isoformat(),
                "assignment": assignment,
                "summary": {
                    "answer": str(run.result.get("answer", ""))[:1_000],
                    "termination_reason": str(run.result.get("termination_reason", "")),
                    "waiting_for_approval": run.status == "WAITING_APPROVAL",
                },
            }
            for run, assignment in assignments
        ]
    }


@router.get("/review/runs/{run_id}")
def get_review_run(
    run_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """读取单项审查投影；同时验证 Review scope 与显式资源关系。"""
    tenant_id, user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    if "agent:review" not in permissions:
        raise HTTPException(status_code=403, detail="agent:review permission is required")
    store = request.app.state.container.run_store
    assignment = store.review_assignment(tenant_id, run_id, user_id)
    if assignment is None:
        # 与不存在使用同一响应，避免攻击者通过 Review 详情枚举同租户 Run。
        raise HTTPException(status_code=404, detail="run not found")
    run = store.get(tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _review_projection(run, assignment)


@router.get("/review/runs/{run_id}/evidence/{evidence_id}")
def get_review_evidence(
    run_id: str,
    evidence_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """在 Assignment 与数据域权限双重校验后返回一条限长、已脱敏证据正文。"""
    tenant_id, user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    if "agent:review" not in permissions or "evidence:content:read" not in permissions:
        raise HTTPException(status_code=403, detail="evidence content permission is required")
    store = request.app.state.container.run_store
    if not store.is_assigned_reviewer(tenant_id, run_id, user_id):
        raise HTTPException(status_code=404, detail="evidence not found")
    run = store.get(tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="evidence not found")
    evidence_items = run.result.get("evidence", []) if isinstance(run.result, dict) else []
    for item in evidence_items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("evidence_id", item.get("id", item.get("document_id", ""))))
        if item_id != evidence_id:
            continue
        data_domain = str(item.get("data_domain", item.get("knowledge_base", ""))).strip()
        if data_domain and f"data-domain:{data_domain}:read" not in permissions:
            raise HTTPException(status_code=403, detail="evidence data-domain permission is required")
        content = str(item.get("content", item.get("text", item.get("snippet", ""))))
        return {
            "evidence_id": item_id,
            "source": str(item.get("source", item.get("title", ""))),
            "data_domain": data_domain,
            "content": content[:12_000],
            "truncated": len(content) > 12_000,
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        }
    raise HTTPException(status_code=404, detail="evidence not found")


@router.get("/review/runs/{run_id}/comments")
def list_review_comments(
    run_id: str,
    request: Request,
    limit: int = 100,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """读取当前显式审查关系下的协作备注；不能通过角色名浏览他人讨论。"""
    tenant_id, user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    if "agent:review" not in permissions:
        raise HTTPException(status_code=403, detail="agent:review permission is required")
    try:
        comments = request.app.state.container.run_store.list_review_comments(
            tenant_id, run_id, user_id, limit=min(max(limit, 1), 200)
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    return {"items": comments}


@router.post("/review/runs/{run_id}/comments", status_code=status.HTTP_201_CREATED)
def add_review_comment(
    run_id: str,
    payload: ReviewCommentRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """由当前审查人写入协作备注；转交后旧 Assignment 自动失去写入权。"""
    tenant_id, user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    if "agent:review" not in permissions or "run:review:comment" not in permissions:
        raise HTTPException(status_code=403, detail="review comment permission is required")
    try:
        return request.app.state.container.run_store.add_review_comment(
            tenant_id, run_id, user_id, payload.message
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


@router.get("/runs/{run_id}/audit-events")
def get_run_audit_events(
    run_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
    after_sequence: int = 0,
    limit: int = 1_000,
) -> dict:
    """为所有者或显式租户观察员读取 Governance 审计事实，治理不可用时明确失败。"""
    tenant_id, user_id, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    container = request.app.state.container
    run = container.run_store.get(tenant_id, run_id)
    if run is None or not _can_read_run(container.run_store, tenant_id, run, user_id, permissions):
        raise HTTPException(status_code=404, detail="run not found")
    if not container.settings.governance_base_url:
        return {"items": [], "status": "unconfigured"}
    try:
        # 审计读取与事件发布使用同一工作负载身份和专属 mTLS 证书，不能直连绕过生产传输边界。
        headers = {"X-Tenant-Id": tenant_id, "X-Governance-Event-Key": container.settings.governance_event_key}
        if container.workload_identity is not None:
            headers.update(container.workload_identity.authorization_header())
        response = httpx.get(
            f"{container.settings.governance_base_url.rstrip('/')}/internal/v1/governance/audit-events/runs/{run_id}",
            headers=headers,
            params={"after_sequence": max(0, after_sequence), "limit": min(max(limit, 1), 1_000)},
            timeout=container.settings.service_http_timeout,
            **container._mtls_options(),
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="governance audit is unavailable") from exc
    return {**response.json(), "status": "available"}


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    after_sequence: int = 0,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> StreamingResponse:
    """以 SSE 从已提交 Session Ledger 流式输出单个 Run 事件，不依赖单进程 Event Bus。"""
    if after_sequence < 0:
        raise HTTPException(status_code=422, detail="after_sequence must be non-negative")
    x_tenant_id, x_user_id, permissions = _trusted_identity(request, x_tenant_id, x_user_id, x_permissions)
    store = request.app.state.container.run_store
    run = store.get(x_tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if not _can_read_run(store, x_tenant_id, run, x_user_id, permissions):
        raise HTTPException(status_code=404, detail="run not found")

    async def event_stream():
        """轮询共享账本而非本地回调，使多副本 Runtime 的 SSE 恢复保持正确。"""
        cursor = after_sequence
        # 长连接断开/重连由客户端以 Last-Event-ID 或 after_sequence 恢复；这里不保存订阅状态。
        while True:
            events = store.session_events(
                x_tenant_id, run.context.session_id, after_sequence=cursor, limit=1_000
            )
            for event in events:
                cursor = event.sequence
                if event.run_id != run_id:
                    continue
                payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
                yield f"id: {event.sequence}\nevent: {event.event_type.value}\ndata: {payload}\n\n"
            current = store.get(x_tenant_id, run_id)
            if current is None or current.runtime_state in {"COMPLETED", "FAILED", "CANCELLED"}:
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/inputs", status_code=status.HTTP_202_ACCEPTED)
def enqueue_run_input(
    run_id: str,
    payload: AgentRunInputRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
) -> dict:
    """把 Steering/Follow-up 先写入 Context，再以引用投入 RunMailbox，禁止 Runtime 保存正文副本。"""
    x_tenant_id, x_user_id, _ = _trusted_identity(request, x_tenant_id, x_user_id, "")
    container = request.app.state.container
    run = container.run_store.get(x_tenant_id, run_id)
    if run is None or run.user_id != x_user_id:
        raise HTTPException(status_code=404, detail="run not found")
    if run.runtime_state in {"COMPLETED", "FAILED", "CANCELLED"}:
        raise HTTPException(status_code=409, detail="terminal run rejects mailbox input")
    request_id = x_request_id or f"mailbox-{uuid4().hex}"
    # Context 是原始会话正文的唯一所有者；邮箱只保存无敏感正文的领取引用。
    _capability(container, RuntimeCapability.CONTEXT).append_message(
        run.context.session_id,
        ConversationMessage(
            role="user",
            content=payload.message,
            metadata={
                "source": "runtime-mailbox",
                "input_type": payload.input_type,
                "request_id": request_id,
                "idempotency_key": request_id,
            },
        ),
        x_tenant_id,
        x_user_id,
    )
    try:
        message_id = container.run_store.enqueue_mailbox_input(
            x_tenant_id,
            run_id,
            RunMailboxInputType(payload.input_type),
            idempotency_key=request_id,
        )
    except (LookupError, InvalidRunTransition) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if run.status == "WAITING_INPUT":
        claimed = container.run_store.claim_mailbox_input(x_tenant_id, run_id)
        if claimed is None or claimed.message_id != message_id:
            raise HTTPException(status_code=409, detail="mailbox input is unavailable for resume")
        return resume_run(
            run_id,
            AgentResumeRequest(approved=True),
            request,
            x_tenant_id=x_tenant_id,
            x_user_id=x_user_id,
            _user_input=UserInputResume(
                message_id=claimed.message_id, lease_token=claimed.lease_token
            ),
        )
    return {"run_id": run_id, "message_id": message_id, "status": "QUEUED"}


@router.get("/sessions/{session_id}/events")
def get_session_events(
    session_id: str,
    request: Request,
    after_sequence: int = 0,
    limit: int = 200,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> dict:
    """按租户读取受限 Session 事件流，返回脱敏模型投影而不泄露原始数据域正文。"""
    x_tenant_id, x_user_id, _ = _trusted_identity(request, x_tenant_id, x_user_id, "")
    header = request.app.state.container.run_store.session_header(x_tenant_id, session_id)
    if header is None or header.owner_id != x_user_id:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        events = request.app.state.container.run_store.session_events(
            x_tenant_id,
            session_id,
            after_sequence=after_sequence,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "session_id": session_id,
        "events": [event.model_dump(mode="json") for event in events],
        "next_after_sequence": events[-1].sequence if events else after_sequence,
    }


@router.get("/sessions/{session_id}")
def get_session(
    session_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> dict:
    """读取会话不可变 Header 与可再生 Projection，不返回其他数据域的原始正文。"""
    x_tenant_id, x_user_id, _ = _trusted_identity(request, x_tenant_id, x_user_id, "")
    store = request.app.state.container.run_store
    header = store.session_header(x_tenant_id, session_id)
    if header is None or header.owner_id != x_user_id:
        raise HTTPException(status_code=404, detail="session not found")
    projection = store.session_projection(x_tenant_id, session_id)
    return {
        "header": header.model_dump(mode="json"),
        "projection": projection.model_dump(mode="json") if projection else None,
    }


@router.post("/sessions/{source_session_id}/fork", status_code=status.HTTP_201_CREATED)
def fork_session(
    source_session_id: str,
    payload: SessionForkRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> dict:
    """创建只继承父事件前缀的会话分支，适用于安全回放和人工方案分叉。"""
    x_tenant_id, x_user_id, _ = _trusted_identity(request, x_tenant_id, x_user_id, "")
    store = request.app.state.container.run_store
    source = store.session_header(x_tenant_id, source_session_id)
    if source is None or source.owner_id != x_user_id:
        raise HTTPException(status_code=404, detail="source session not found")
    try:
        header = store.fork_session(
            tenant_id=x_tenant_id,
            source_session_id=source_session_id,
            new_session_id=payload.session_id,
            owner_id=x_user_id,
            agent_id=source.agent_id,
            agent_version=source.agent_version,
            snapshot_id=source.snapshot_id,
            seed_sequence=payload.seed_sequence,
            delegation_depth=source.delegation_depth + 1,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return header.model_dump(mode="json")


@router.post("/sessions/{session_id}/compact")
def compact_session(
    session_id: str,
    payload: SessionCompactionRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """追加会话压缩替换事件；需要显式管理权限，且不会删除原始审计事实。"""
    x_tenant_id, x_user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    if "agent:session:manage" not in permissions:
        raise HTTPException(status_code=403, detail="agent:session:manage permission is required")
    store = request.app.state.container.run_store
    header = store.session_header(x_tenant_id, session_id)
    if header is None or header.owner_id != x_user_id:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        event = store.compact_session(
            x_tenant_id,
            session_id,
            replaced_through_sequence=payload.replaced_through_sequence,
            summary=model_visible_message("system", payload.summary, source="session.compaction"),
            policy_version=payload.policy_version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.container.publish_session_event(event)
    return event.model_dump(mode="json")


@router.post("/sessions/{session_id}/archive")
def archive_session(
    session_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="", alias="X-Permissions"),
) -> dict:
    """将会话账本导出至配置的对象存储；在线库仅返回定位键与完整性摘要。"""
    x_tenant_id, x_user_id, permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    if "agent:session:archive" not in permissions:
        raise HTTPException(status_code=403, detail="agent:session:archive permission is required")
    header = request.app.state.container.run_store.session_header(x_tenant_id, session_id)
    if header is None or header.owner_id != x_user_id:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        return request.app.state.container.archive_session(x_tenant_id, session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> dict:
    """请求协作取消；运行中的外部调用将在下一守卫节点停止，队列任务立即标记。"""
    x_tenant_id, x_user_id, _ = _trusted_identity(request, x_tenant_id, x_user_id, "")
    existing = request.app.state.container.run_store.get(x_tenant_id, run_id)
    # 取消会改变执行状态且可能停止正在使用预算的工作流，因此不能因知道 run_id
    # 就操作同租户其他用户的任务。团队取消应以后续显式委派/应急权限端点实现。
    if existing is None or existing.user_id != x_user_id:
        raise HTTPException(status_code=404, detail="run not found")
    run = request.app.state.container.agent_harness.cancel(x_tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run.model_dump(mode="json") if hasattr(run, "model_dump") else run

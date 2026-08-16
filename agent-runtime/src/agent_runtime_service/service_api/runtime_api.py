from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request, status
from platform_sdk.contracts.capabilities import RuntimeCapability
from platform_sdk.contracts.context import ConversationMessage
from platform_sdk.contracts.runtime_api import (
    AgentFollowupRequest,
    AgentResumeRequest,
    AgentRunRequest,
)

from agent_runtime_service.runtime.capabilities import CapabilityUnavailable
from agent_runtime_service.runtime.models import ApprovalResume, RuntimeBudget
from agent_runtime_service.runtime.session_events import RuntimeEventType, model_visible_message
from agent_runtime_service.runtime.snapshot_compiler import CompiledAgentPlan, SnapshotCompileError

router = APIRouter(prefix="/agent", tags=["agent-runtime"])


def _public_result(result: dict) -> dict:
    """移除仅用于恢复和内部编排的下划线字段，禁止它们穿透 Runtime API。"""
    return {key: value for key, value in result.items() if not key.startswith("_")}


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
        "capabilities": list(container.capabilities.names),
        "capability_manifests": [
            item.model_dump(mode="json") for item in container.capabilities.manifests
        ],
    }


def _capability(container, capability: RuntimeCapability):
    """把未部署能力转为 503，避免接口因内部属性缺失出现不透明错误。"""
    try:
        return container.capability(capability)
    except CapabilityUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


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
    settings = request.app.state.container.settings
    claims = request.scope.get("auth.claims")
    if settings.oidc_enabled and not isinstance(claims, dict):
        raise HTTPException(status_code=401, detail="verified OIDC identity is required")
    if settings.oidc_enabled:
        # OIDC middleware has already deleted caller values and rebuilt these
        # headers from configurable claim mappings. Reading the rebuilt values
        # keeps Runtime compatible with enterprise-specific tenant/user claims.
        permissions = {item.strip() for item in permissions_header.split(",") if item.strip()}
    else:
        permissions = {item.strip() for item in permissions_header.split(",") if item.strip()}
    if not tenant_id or not user_id:
        raise HTTPException(status_code=401, detail="trusted tenant and user identity are required")
    return tenant_id, user_id, permissions


@router.post("/run")
def run_agent(
    payload: AgentRunRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    x_permissions: str = Header(default="rag:read", alias="X-Permissions"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    x_trace_id: str | None = Header(default=None, alias="X-Trace-Id"),
    x_run_id: str | None = Header(default=None, alias="X-Run-Id"),
    _temporal_worker_execution: bool = False,
    _release_resolution: dict | None = None,
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
    if "rag:read" not in permissions:
        raise HTTPException(status_code=403, detail="rag:read permission is required")
    request_id = x_request_id or f"agent-{uuid4().hex}"
    session_id = payload.session_id or request_id
    trace_id = x_trace_id or request_id
    try:
        # Harness 仅协调发布解析和快照加载; API 不再直接调用 Control Plane 或编译快照。
        resolution = _release_resolution or container.agent_harness.resolve_release(
            tenant_id=x_tenant_id,
            user_id=x_user_id,
            agent_id=payload.agent_id,
            environment=payload.environment,
            session_id=session_id,
            trace_id=trace_id,
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
    compiled_plan = loaded_snapshot.plan
    if compiled_plan.executor_profile == "temporal-workflow/v1" and not _temporal_worker_execution:
        raise HTTPException(
            status_code=409,
            detail="temporal-workflow/v1 releases must be submitted through POST /runs",
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
        deadline_seconds=deadline_seconds,
        attempt_budget=(
            payload.attempt_budget
            if payload.attempt_budget is not None
            else container.settings.agent_attempt_budget
        ),
        run_id=x_run_id,
        parent_run_id=str(payload.metadata.get("_parent_run_id", "")),
    )
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
        )
        container.publish_session_event(turn_event)
    try:
        if not run_already_started:
            user_event = container.run_store.append_session_event(
                execution,
                RuntimeEventType.USER_MESSAGE,
                metadata={"message_source": "runtime-api"},
                model_message=model_visible_message(
                    "user", payload.task, source="runtime-api.user-message"
                ),
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
                "run_id": execution.run_id,
                "trace_id": execution.trace_id,
                "agent_id": execution.agent_id,
                "agent_version": execution.agent_version,
                "snapshot_id": execution.snapshot_id,
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
            )
            container.publish_session_event(assistant_event)
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
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    x_trace_id: str | None = Header(default=None, alias="X-Trace-Id"),
) -> dict:
    """把运行提交给持久化异步队列并返回 202；同租户 request_id 重试保持幂等。"""
    x_tenant_id, x_user_id, trusted_permissions = _trusted_identity(
        request, x_tenant_id, x_user_id, x_permissions
    )
    if "rag:read" not in trusted_permissions:
        raise HTTPException(status_code=403, detail="rag:read permission is required")
    request_id = x_request_id or f"agent-{uuid4().hex}"
    # Durable Profile 必须在提交时就被识别, 防止同步 API 在 Worker 外绕开 Temporal。
    container = request.app.state.container
    resolution = container.agent_harness.resolve_release(
        tenant_id=x_tenant_id,
        user_id=x_user_id,
        agent_id=payload.agent_id,
        environment=payload.environment,
        session_id=payload.session_id or request_id,
        trace_id=x_trace_id or request_id,
    )
    loaded = container.agent_harness.load_snapshot(
        resolution, tenant_id=x_tenant_id, agent_id=payload.agent_id
    )
    if loaded.plan.executor_profile != "temporal-workflow/v1":
        raise HTTPException(
            status_code=409,
            detail="asynchronous /runs is reserved for temporal-workflow/v1 releases",
        )
    return _capability(container, RuntimeCapability.WORKFLOW).submit(
        {
            "payload": payload.model_dump(mode="json"),
            "tenant_id": x_tenant_id,
            "user_id": x_user_id,
            "permissions": ",".join(sorted(trusted_permissions)),
            "request_id": request_id,
            "trace_id": x_trace_id or request_id,
            "data_region": loaded.plan.data_region,
            # 提交时冻结 Resolve 结果; Worker 绝不能在 Release 切换后重新选择版本。
            "release_resolution": resolution,
        }
    )


@router.post("/runs/{run_id}/resume")
def resume_run(
    run_id: str,
    payload: AgentResumeRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
    _temporal_worker_execution: bool = False,
) -> dict:
    """恢复等待审批的检查点。

    只允许原租户且状态为 WAITING_APPROVAL 的运行恢复；批准/拒绝都会生成新的治理
    事件并在终态时写回会话记忆。
    """
    x_tenant_id, x_user_id, _ = _trusted_identity(request, x_tenant_id, x_user_id, "")
    container = request.app.state.container
    run = container.run_store.get(x_tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status != "WAITING_APPROVAL":
        raise HTTPException(status_code=409, detail="run is not waiting for approval")
    if run.cancel_requested:
        raise HTTPException(status_code=409, detail="run was cancelled")
    budget = run.result.get("budget", {})
    max_steps = int(budget.get("max_steps", container.settings.agent_max_steps))
    try:
        compiled_plan = CompiledAgentPlan.model_validate(
            run.result.get("_compiled_plan")
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail="persisted execution plan is unavailable") from exc
    if compiled_plan.executor_profile == "temporal-workflow/v1" and not _temporal_worker_execution:
        queue = _capability(container, RuntimeCapability.WORKFLOW)
        if not hasattr(queue, "resume"):
            raise HTTPException(status_code=503, detail="Temporal durable executor is unavailable")
        queued = queue.resume(
            x_tenant_id,
            run_id,
            {
                "approved": payload.approved,
                "approval_id": payload.approval_id,
                "reason": payload.reason,
                "decided_by": x_user_id,
            },
        )
        if queued is None:
            raise HTTPException(status_code=409, detail="durable workflow is no longer available")
        return queued
    result = container.agent_harness.resume(
        run_id,
        ApprovalResume(
            approved=payload.approved,
            approval_id=payload.approval_id,
            decided_by=x_user_id,
            reason=payload.reason,
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
        )
        container.publish_session_event(assistant_event)
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
    if target_plan is not None and target_plan.executor_profile == "temporal-workflow/v1":
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
) -> dict:
    """读取租户范围内的运行或异步队列状态，不返回其他租户的检查点/提交内容。"""
    x_tenant_id, _, _ = _trusted_identity(request, x_tenant_id, "anonymous", "")
    run = request.app.state.container.run_store.get(x_tenant_id, run_id)
    if run is None:
        queued = _capability(
            request.app.state.container, RuntimeCapability.WORKFLOW
        ).get(x_tenant_id, run_id)
        if queued is None:
            raise HTTPException(status_code=404, detail="run not found")
        return queued
    body = run.model_dump(mode="json")
    body["result"] = _public_result(body.get("result") or {})
    return body


@router.get("/sessions/{session_id}/events")
def get_session_events(
    session_id: str,
    request: Request,
    after_sequence: int = 0,
    limit: int = 200,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
) -> dict:
    """按租户读取受限 Session 事件流，返回脱敏模型投影而不泄露原始数据域正文。"""
    x_tenant_id, _, _ = _trusted_identity(request, x_tenant_id, "anonymous", "")
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


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
) -> dict:
    """请求协作取消；运行中的外部调用将在下一守卫节点停止，队列任务立即标记。"""
    x_tenant_id, _, _ = _trusted_identity(request, x_tenant_id, "anonymous", "")
    run = request.app.state.container.agent_harness.cancel(x_tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run.model_dump(mode="json") if hasattr(run, "model_dump") else run

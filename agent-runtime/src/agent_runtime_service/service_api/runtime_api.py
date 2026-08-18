import asyncio
import json
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from platform_sdk.contracts.capabilities import RuntimeCapability
from platform_sdk.contracts.context import ConversationMessage
from platform_sdk.contracts.runtime_api import (
    AgentFollowupRequest,
    AgentResumeRequest,
    AgentRunInputRequest,
    AgentRunRequest,
    SessionCompactionRequest,
    SessionForkRequest,
)

from agent_runtime_service.runtime.capabilities import CapabilityUnavailable
from agent_runtime_service.runtime.mailbox import ClaimedRunMailboxItem, RunMailboxInputType
from agent_runtime_service.runtime.models import (
    ApprovalResume,
    ExecutionLifecycle,
    RuntimeBudget,
    UserInputResume,
)
from agent_runtime_service.runtime.run_state import AgentRunEvent, InvalidRunTransition
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


def _capability(container, capability: RuntimeCapability):
    """把未部署能力转为 503，避免接口因内部属性缺失出现不透明错误。"""
    try:
        return container.capability(capability)
    except CapabilityUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _is_durable_plan(plan: CompiledAgentPlan) -> bool:
    """以编译后的生命周期而非历史 Profile 名称判断是否必须走 Temporal。"""
    return plan.execution_requirements.lifecycle == ExecutionLifecycle.DURABLE_WORKFLOW


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
        deadline_seconds=deadline_seconds,
        attempt_budget=(
            payload.attempt_budget
            if payload.attempt_budget is not None
            else container.settings.agent_attempt_budget
        ),
        run_id=x_run_id,
        parent_run_id=str(payload.metadata.get("_parent_run_id", "")),
        parent_session_id=str(payload.metadata.get("_parent_session_id", "")),
        root_task_id=str(payload.metadata.get("_root_task_id", "")),
        collaboration_snapshot_id=str(payload.metadata.get("_collaboration_snapshot_id", "")),
        business_operation_id=str(payload.metadata.get("_business_operation_id", "")),
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
            raise HTTPException(status_code=409, detail={"code": "invalid_run_state", "message": str(exc)}) from exc
        if state_event is not None:
            container.publish_session_event(state_event)
    if run_already_started:
        try:
            container.reconcile_tool_intents(execution)
        except RuntimeError as exc:
            # 对不确定副作用 fail-closed；Temporal Activity 会按既有策略稍后恢复，
            # 同步调用方得到明确的“等待对账”而不是偷偷重复调用业务系统。
            raise HTTPException(status_code=409, detail={"code": "tool_recovery_pending", "message": str(exc)}) from exc
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
    if not _is_durable_plan(loaded.plan):
        raise HTTPException(
            status_code=409,
            detail="asynchronous /runs is reserved for durable-workflow releases",
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
    x_permissions: str = Header(default="", alias="X-Permissions"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    _temporal_worker_execution: bool = False,
    _user_input: UserInputResume | None = None,
    _claimed_control: ClaimedRunMailboxItem | None = None,
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
    expected_status = "WAITING_INPUT" if _user_input is not None else "WAITING_APPROVAL"
    if run.status != expected_status:
        raise HTTPException(status_code=409, detail=f"run is not waiting for {expected_status.lower()}")
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
            raise HTTPException(status_code=409, detail="approval inbox input is unavailable for resume")
    effective_payload = (
        AgentResumeRequest.model_validate(claimed_control.control_input)
        if claimed_control is not None
        else payload
    )
    budget = run.result.get("budget", {})
    max_steps = int(budget.get("max_steps", container.settings.agent_max_steps))
    try:
        compiled_plan = CompiledAgentPlan.model_validate(
            run.result.get("_compiled_plan")
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail="persisted execution plan is unavailable") from exc
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
        {"mailbox_message_id": _user_input.message_id, "input_type": RunMailboxInputType.STEERING.value}
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
            AgentRunEvent.STEERING_RECEIVED if _user_input is not None else AgentRunEvent.APPROVAL_RECEIVED,
            metadata=input_metadata,
        )
    except InvalidRunTransition as exc:
        raise HTTPException(status_code=409, detail={"code": "invalid_run_state", "message": str(exc)}) from exc
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
        raise HTTPException(status_code=409, detail="approval inbox lease was lost before acknowledgement")
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


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    after_sequence: int = 0,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
) -> StreamingResponse:
    """以 SSE 从已提交 Session Ledger 流式输出单个 Run 事件，不依赖单进程 Event Bus。"""
    if after_sequence < 0:
        raise HTTPException(status_code=422, detail="after_sequence must be non-negative")
    x_tenant_id, _, _ = _trusted_identity(request, x_tenant_id, "anonymous", "")
    store = request.app.state.container.run_store
    run = store.get(x_tenant_id, run_id)
    if run is None:
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
            _user_input=UserInputResume(message_id=claimed.message_id, lease_token=claimed.lease_token),
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
) -> dict:
    """请求协作取消；运行中的外部调用将在下一守卫节点停止，队列任务立即标记。"""
    x_tenant_id, _, _ = _trusted_identity(request, x_tenant_id, "anonymous", "")
    run = request.app.state.container.agent_harness.cancel(x_tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run.model_dump(mode="json") if hasattr(run, "model_dump") else run

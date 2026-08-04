from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.contracts.context import ConversationMessage
from app.contracts.execution import ExecutionContext
from app.domain.schemas import AgentResumeRequest, AgentRunRequest
from app.runtime.models import ApprovalResume, RuntimeBudget
from app.runtime.snapshot_compiler import SnapshotCompileError, compile_snapshot

router = APIRouter(prefix="/agent", tags=["agent-runtime"])


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
) -> dict:
    started = monotonic()
    container = request.app.state.container
    if not container.settings.agent_enabled:
        raise HTTPException(status_code=503, detail="agent graph is disabled")
    permissions = {item.strip() for item in x_permissions.split(",") if item.strip()}
    if "rag:read" not in permissions:
        raise HTTPException(status_code=403, detail="rag:read permission is required")
    request_id = x_request_id or f"agent-{uuid4().hex}"
    session_id = payload.session_id or request_id
    trace_id = x_trace_id or request_id
    resolution = None
    if container.control_plane is not None:
        # Resolution returns an immutable published snapshot.  The graph never
        # executes a mutable draft or looks up policy again mid-run.
        resolution = container.control_plane.resolve(
            tenant_id=x_tenant_id,
            user_id=x_user_id,
            agent_id=payload.agent_id,
            environment=payload.environment,
            session_id=session_id,
            trace_id=trace_id,
        )
    elif container.settings.runtime_snapshot_required:
        raise HTTPException(
            status_code=503, detail="control-plane snapshot resolution is required"
        )
    snapshot = (resolution or {}).get("snapshot", {})
    snapshot_id = (resolution or {}).get("version_id", "local-unversioned")
    agent_version = snapshot.get("agent_version", "local-unversioned")
    try:
        compiled_plan = compile_snapshot(
            snapshot,
            tenant_id=x_tenant_id,
            agent_id=payload.agent_id,
            fallback_model=container.settings.agent_model,
        )
    except SnapshotCompileError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "snapshot_not_executable", "message": str(exc)},
        ) from exc
    runtime_limits = snapshot.get("spec", {}).get("runtime_limits", {})
    configured_steps = payload.max_steps or container.settings.agent_max_steps
    max_steps = min(
        configured_steps, int(runtime_limits.get("max_steps", configured_steps))
    )
    configured_deadline = (
        payload.deadline_seconds or container.settings.agent_deadline_seconds
    )
    deadline_seconds = min(
        configured_deadline,
        int(runtime_limits.get("max_execution_seconds", configured_deadline)),
    )
    execution = ExecutionContext.create(
        request_id=request_id,
        trace_id=trace_id,
        session_id=session_id,
        tenant_id=x_tenant_id,
        user_id=x_user_id,
        agent_id=payload.agent_id,
        agent_version=agent_version,
        snapshot_id=snapshot_id,
        graph_version=snapshot.get("graph_version", "runtime-planner-v1"),
        model_policy_version=snapshot.get("model_policy_version", "local-unversioned"),
        deadline_seconds=deadline_seconds,
        attempt_budget=(
            payload.attempt_budget
            if payload.attempt_budget is not None
            else container.settings.agent_attempt_budget
        ),
        run_id=x_run_id,
    )
    created = container.run_store.create(execution)
    if created.run_id != execution.run_id:
        return {
            **created.result,
            "run_id": created.run_id,
            "snapshot_id": created.snapshot_id,
            "status": created.status,
            "idempotent_replay": True,
        }
    existing = container.run_store.get(x_tenant_id, execution.run_id)
    if existing and existing.cancel_requested:
        raise HTTPException(
            status_code=409, detail="run was cancelled before execution"
        )
    try:
        container.context_client.append_message(
            session_id,
            ConversationMessage(
                role="user", content=payload.task, metadata=payload.metadata
            ),
            x_tenant_id,
            x_user_id,
        )
        max_cost = min(
            payload.max_cost_usd or container.settings.agent_max_cost_usd,
            float(
                runtime_limits.get(
                    "max_cost_usd", container.settings.agent_max_cost_usd
                )
            ),
        )
        budget = RuntimeBudget(
            deadline_at=execution.deadline_at,
            max_steps=max_steps,
            max_llm_calls=int(
                runtime_limits.get(
                    "max_llm_calls", container.settings.agent_max_llm_calls
                )
            ),
            max_tool_calls=int(
                runtime_limits.get(
                    "max_tool_calls", container.settings.agent_max_tool_calls
                )
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
            },
            execution.run_id,
        )
        if result.status != "WAITING_APPROVAL":
            container.context_client.append_message(
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
    except Exception as exc:
        event = container.governance.event_for_run(
            execution,
            "FAILED",
            {},
            type(exc).__name__,
        )
        container.run_store.finish_and_enqueue(
            execution.run_id,
            "FAILED",
            {},
            event,
            type(exc).__name__,
        )
        container.governance.flush()
        raise
    body = {
        **result.model_dump(mode="json"),
        "run_id": execution.run_id,
        "snapshot_id": execution.snapshot_id,
        "latency_ms": int((monotonic() - started) * 1_000),
    }
    event = container.governance.event_for_run(execution, result.status, body)
    container.run_store.finish_and_enqueue(execution.run_id, result.status, body, event)
    container.governance.flush()
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
    request_id = x_request_id or f"agent-{uuid4().hex}"
    return request.app.state.container.async_runs.submit(
        {
            "payload": payload.model_dump(mode="json"),
            "tenant_id": x_tenant_id,
            "user_id": x_user_id,
            "permissions": x_permissions,
            "request_id": request_id,
            "trace_id": x_trace_id or request_id,
        }
    )


@router.post("/runs/{run_id}/resume")
def resume_run(
    run_id: str,
    payload: AgentResumeRequest,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
    x_user_id: str = Header(default="anonymous", alias="X-User-Id"),
) -> dict:
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
    result = container.agent_harness.resume(
        run_id,
        ApprovalResume(
            approved=payload.approved,
            approval_id=payload.approval_id,
            decided_by=x_user_id,
            reason=payload.reason,
        ),
        max_steps=max_steps,
        agent_id=run.context.agent_id,
    )
    body = {
        **result.model_dump(mode="json"),
        "run_id": run_id,
        "snapshot_id": run.snapshot_id,
        "latency_ms": int((datetime.now(UTC) - run.created_at).total_seconds() * 1_000),
    }
    if result.status != "WAITING_APPROVAL":
        container.context_client.append_message(
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
    event = container.governance.event_for_run(run.context, result.status, body)
    container.run_store.finish_and_enqueue(run_id, result.status, body, event)
    container.governance.flush()
    return body


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
) -> dict:
    run = request.app.state.container.run_store.get(x_tenant_id, run_id)
    if run is None:
        queued = request.app.state.container.async_runs.get(x_tenant_id, run_id)
        if queued is None:
            raise HTTPException(status_code=404, detail="run not found")
        return queued
    return run.model_dump(mode="json")


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default", alias="X-Tenant-Id"),
) -> dict:
    run = request.app.state.container.run_store.cancel(x_tenant_id, run_id)
    if run is None:
        queued = request.app.state.container.async_runs.cancel(x_tenant_id, run_id)
        if queued is None:
            raise HTTPException(status_code=404, detail="run not found")
        return queued
    return run.model_dump(mode="json")

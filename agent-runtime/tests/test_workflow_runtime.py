from platform_sdk.contracts.execution import ExecutionContext
from platform_sdk.contracts.skills import OrchestrationOwner
from platform_sdk.contracts.workflow import CompiledWorkflowPlan

from agent_runtime_service.runtime.workflow_runtime import ZeroAgentWorkflowRuntime


class Dispatcher:
    def dispatch(self, capability_id, payload, context):
        assert context.orchestration_owner == OrchestrationOwner.WORKFLOW
        return {"capability": capability_id, "ok": True, "input": payload["input"]}


class SuspendingDispatcher(Dispatcher):
    def __init__(self):
        self.calls = 0

    def dispatch(self, capability_id, payload, context):
        from agent_runtime_service.runtime.workflow_runtime import WorkflowSuspended

        self.calls += 1
        if capability_id == "HUMAN_APPROVAL" and not payload.get("signal"):
            raise WorkflowSuspended("approval_required", {"approval_id": "approval-1"})
        return super().dispatch(capability_id, payload, context)


def test_zero_agent_workflow_executes_fixed_capability_steps():
    context = ExecutionContext.create(
        request_id="req",
        trace_id="trace",
        session_id="workflow-run",
        tenant_id="tenant",
        user_id="user",
        agent_id="",
        agent_version="",
        snapshot_id="workflow-snapshot",
        deadline_seconds=30,
        attempt_budget=2,
        orchestration_owner=OrchestrationOwner.WORKFLOW,
        workflow_id="nightly-scan",
    )
    plan = CompiledWorkflowPlan(
        workflow_id="nightly-scan",
        version="1.0.0",
        steps=[
            {"step_id": "parse", "capability_id": "DOCUMENT_PARSE"},
            {"step_id": "scan", "capability_id": "CONSISTENCY_SCAN"},
        ],
    )
    result = ZeroAgentWorkflowRuntime(Dispatcher()).run(plan, {"batch": "b1"}, context)
    assert result.status == "COMPLETED"
    assert list(result.step_outputs) == ["parse", "scan"]


def test_zero_agent_workflow_persists_waiting_cursor_and_resumes_same_step():
    context = ExecutionContext.create(
        request_id="req",
        trace_id="trace",
        session_id="workflow-run",
        tenant_id="tenant",
        user_id="user",
        agent_id="",
        agent_version="",
        snapshot_id="workflow-snapshot",
        deadline_seconds=30,
        attempt_budget=2,
        orchestration_owner=OrchestrationOwner.WORKFLOW,
        workflow_id="approval-flow",
    )
    plan = CompiledWorkflowPlan(
        workflow_id="approval-flow",
        version="1.0.0",
        steps=[{"step_id": "approve", "capability_id": "HUMAN_APPROVAL"}],
    )
    runtime = ZeroAgentWorkflowRuntime(SuspendingDispatcher())
    waiting = runtime.run(plan, {"case": "1"}, context)
    assert waiting.status == "WAITING_SIGNAL"
    assert waiting.next_step_index == 0
    completed = runtime.run(
        plan,
        {"case": "1"},
        context,
        checkpoint=waiting,
        signal={"approved": True},
    )
    assert completed.status == "COMPLETED"


def test_durable_workflow_checkpoints_after_each_frozen_step():
    """Temporal Activity 边界只推进一步，恢复时不重放已完成 Provider。"""
    context = ExecutionContext.create(
        request_id="req",
        trace_id="trace",
        session_id="workflow-run",
        tenant_id="tenant",
        user_id="user",
        agent_id="",
        agent_version="",
        snapshot_id="workflow-snapshot",
        deadline_seconds=30,
        attempt_budget=2,
        orchestration_owner=OrchestrationOwner.WORKFLOW,
        workflow_id="durable-flow",
    )
    plan = CompiledWorkflowPlan(
        workflow_id="durable-flow",
        version="1.0.0",
        steps=[
            {"step_id": "first", "capability_id": "FIRST"},
            {"step_id": "second", "capability_id": "SECOND"},
        ],
    )
    runtime = ZeroAgentWorkflowRuntime(Dispatcher())
    checkpoint = runtime.run(plan, {"case": "1"}, context, max_steps=1)
    assert checkpoint.status == "RUNNING"
    assert checkpoint.next_step_index == 1
    assert list(checkpoint.step_outputs) == ["first"]

    completed = runtime.run(
        plan,
        {"case": "1"},
        context,
        checkpoint=checkpoint,
        max_steps=1,
    )
    assert completed.status == "COMPLETED"
    assert list(completed.step_outputs) == ["first", "second"]

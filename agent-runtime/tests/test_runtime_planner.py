from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from langgraph.checkpoint.sqlite import SqliteSaver

from agent_runtime_service.agent.graph import AgentGraph
from agent_runtime_service.agent.models import AgentAction, AgentDecision
from agent_runtime_service.runtime.budget import BudgetGuard
from agent_runtime_service.runtime.models import ApprovalResume, RuntimeBudget
from agent_runtime_service.runtime.planner import HeuristicSemanticAnalyzer, RuntimePlanner


class EmptyContext:
    def assemble(self, request, *, execution_headers=None):
        del execution_headers
        from platform_sdk.contracts.context import ContextPackage

        return ContextPackage(
            session_id=request.session_id,
            knowledge_evidence=[],
            user_context={},
            token_budget=1_000,
            estimated_tokens=0,
        )


class SequenceEngine:
    uses_llm = False

    def __init__(self, decisions: list[AgentDecision]) -> None:
        self.decisions = decisions

    def decide(self, state, tool_registry):
        return self.decisions.pop(0)


class LlmEngine(SequenceEngine):
    uses_llm = True


class ApprovalTool:
    def manifests(self, permissions, **kwargs):
        return [{"name": "payments.refund", "risk": "write_high_risk"}]

    def execute(self, name, arguments, context):
        if not context.approval_id:
            return {
                "status": "PENDING_APPROVAL",
                "approval_id": "approval-001",
                "tool_name": name,
            }
        return {"refund_id": "refund-001", "status": "accepted"}


def state(task: str, *, budget: RuntimeBudget | None = None) -> dict:
    selected_budget = budget or RuntimeBudget(
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        max_steps=5,
        max_llm_calls=5,
        max_tool_calls=2,
        max_retrieval_rounds=2,
        max_cost_usd=1,
    )
    return {
        "task": task,
        "metadata": {},
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "permissions": ["rag:read", "refund:write"],
        "request_id": "request-a",
        "session_id": "session-a",
        "run_id": "run-a",
        "trace_id": "trace-a",
        "agent_id": "agent-a",
        "agent_version": "agent-a:1.0.0",
        "snapshot_id": "version-a",
        "agent_snapshot": {
            "graph_version": "agent-a-graph:1",
            "model_policy_version": "agent-a-policy:1",
            "spec": {
                "knowledge": [{"knowledge_base": "quality-manual", "version": "2026-01"}],
                "tools": [{"tool_name": "payments.refund", "version": "1.0.0"}],
            },
        },
        "deadline_at": selected_budget.deadline_at.isoformat(),
        "attempt_budget_remaining": 4,
        "budget": selected_budget.model_dump(mode="json"),
        "step_count": 0,
        "max_steps": selected_budget.max_steps,
        "observations": [],
        "evidence": [],
        "execution_trace": [],
    }


def test_planner_recognizes_intent_entities_sources_and_route() -> None:
    planner = RuntimePlanner(HeuristicSemanticAnalyzer())
    current = state("审核文档 DOC-123 是否符合要求")
    current.update(planner.analyze(current))
    plan = planner.build_plan(current)

    assert plan.intent.name == "compliance_review"
    assert plan.entities[0].name == "business_id"
    assert plan.entities[0].value == "DOC-123"
    assert plan.source_plan.knowledge_bases == ["quality-manual"]
    assert plan.route.route.value == "rag"
    assert plan.graph_version == "agent-a-graph:1"


def test_llm_call_limit_is_enforced_outside_the_decision_engine() -> None:
    budget = RuntimeBudget(
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        max_steps=5,
        max_llm_calls=0,
        max_tool_calls=2,
        max_retrieval_rounds=2,
        max_cost_usd=1,
    )
    engine = LlmEngine([AgentDecision(action=AgentAction.ANSWER, final_answer="unsafe")])
    graph = AgentGraph(
        engine,
        context_client=EmptyContext(),
        budget_guard=BudgetGuard(0.01, 0.001),
    )

    result = graph.run(state("General question", budget=budget), "thread-limit")

    assert result.status == "LIMIT_EXCEEDED"
    assert result.termination_reason == "MAX_LLM_CALLS"
    assert len(engine.decisions) == 1


def test_downstream_attempt_budget_blocks_calls_before_execution() -> None:
    budget = RuntimeBudget(
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        max_steps=5,
        max_llm_calls=5,
        max_tool_calls=2,
        max_retrieval_rounds=2,
        max_cost_usd=1,
        max_attempts=0,
    )
    engine = SequenceEngine(
        [AgentDecision(action=AgentAction.ANSWER, final_answer="should not run")]
    )
    graph = AgentGraph(
        engine,
        context_client=EmptyContext(),
        budget_guard=BudgetGuard(0.01, 0.001),
    )

    result = graph.run(state("审核文档 DOC-123", budget=budget), "thread-attempt-limit")

    assert result.status == "LIMIT_EXCEEDED"
    assert result.termination_reason == "ATTEMPT_BUDGET_EXCEEDED"
    assert len(engine.decisions) == 1


def test_tool_approval_interrupt_survives_process_restart(tmp_path) -> None:
    checkpoint_path = tmp_path / "checkpoints.db"
    first_connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
    first_saver = SqliteSaver(first_connection)
    first_saver.setup()
    first_engine = SequenceEngine(
        [AgentDecision(action=AgentAction.TOOL, tool_name="payments.refund")]
    )
    first_graph = AgentGraph(
        first_engine,
        tool_registry=ApprovalTool(),
        context_client=EmptyContext(),
        checkpointer=first_saver,
    )

    interrupted = first_graph.run(state("Execute refund for ORD-123"), "durable-thread")
    assert interrupted.status == "WAITING_APPROVAL"
    assert interrupted.interrupts[0]["approval_id"] == "approval-001"
    first_connection.close()

    second_connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
    second_saver = SqliteSaver(second_connection)
    second_saver.setup()
    second_engine = SequenceEngine(
        [AgentDecision(action=AgentAction.ANSWER, final_answer="Refund accepted.")]
    )
    second_graph = AgentGraph(
        second_engine,
        tool_registry=ApprovalTool(),
        context_client=EmptyContext(),
        checkpointer=second_saver,
    )
    resumed = second_graph.resume(
        "durable-thread",
        ApprovalResume(approved=True, approval_id="approval-001", decided_by="reviewer"),
        max_steps=5,
    )

    assert resumed.status == "COMPLETED"
    assert resumed.answer == "Refund accepted."
    tool_observation = next(item for item in resumed.observations if item["type"] == "tool")
    assert tool_observation["result"]["refund_id"] == "refund-001"
    assert resumed.budget["tool_calls"] == 1
    second_connection.close()

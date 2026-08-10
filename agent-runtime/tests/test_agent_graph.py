from platform_sdk.contracts.context import ContextPackage
from platform_sdk.contracts.models import Evidence
from platform_sdk.tools.registry import ToolRegistry

from agent_runtime_service.agent.graph import AgentGraph
from agent_runtime_service.agent.models import AgentAction, AgentDecision


class SequenceDecisionEngine:
    def __init__(self, decisions: list[AgentDecision]) -> None:
        self.decisions = decisions

    def decide(self, state, tool_registry):
        return self.decisions.pop(0)


class FakeContextClient:
    """Contract double: Runtime sees Context only through its public API."""

    def assemble(self, request, *, execution_headers=None):
        del execution_headers
        evidence = (
            [
                Evidence(
                    source_id="reg-1",
                    source_type="regulation",
                    text="Audit records must be attributable and retained.",
                    score=0.93,
                )
            ]
            if request.include_rag
            else []
        )
        return ContextPackage(
            session_id=request.session_id,
            knowledge_evidence=evidence,
            user_context={"tenant_id": request.tenant_id, "user_id": request.user_id},
            token_budget=12_000,
            estimated_tokens=sum(max(1, len(item.text) // 4) for item in evidence),
        )


def initial_state(max_steps: int = 5):
    return {
        "task": "Find the audit record requirement",
        "document_id": None,
        "content": None,
        "metadata": {},
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "permissions": ["rag:read"],
        "request_id": "request-a",
        "step_count": 0,
        "max_steps": max_steps,
        "observations": [],
        "evidence": [],
    }


def test_agent_retrieves_then_answers() -> None:
    engine = SequenceDecisionEngine(
        [
            AgentDecision(action=AgentAction.RETRIEVE, query="audit record retention"),
            AgentDecision(
                action=AgentAction.ANSWER, final_answer="Audit records must be retained [reg-1]."
            ),
        ]
    )
    graph = AgentGraph(engine, ToolRegistry(), context_client=FakeContextClient())

    result = graph.run(initial_state(), "thread-retrieve")

    assert result.termination_reason == "ANSWERED"
    assert result.steps == 2
    assert result.evidence[0]["source_id"] == "reg-1"


def test_agent_stops_at_step_budget() -> None:
    engine = SequenceDecisionEngine(
        [
            AgentDecision(action=AgentAction.RETRIEVE, query="first"),
            AgentDecision(action=AgentAction.RETRIEVE, query="second"),
        ]
    )
    graph = AgentGraph(engine, ToolRegistry(), context_client=FakeContextClient())

    result = graph.run(initial_state(max_steps=2), "thread-limit")

    assert result.termination_reason == "MAX_STEPS"
    assert result.steps == 2


def test_answer_without_evidence_is_blocked() -> None:
    engine = SequenceDecisionEngine(
        [AgentDecision(action=AgentAction.ANSWER, final_answer="An unsupported confident answer")]
    )
    graph = AgentGraph(engine, ToolRegistry(), context_client=FakeContextClient())

    result = graph.run(initial_state(), "thread-no-evidence")

    assert result.answer == "Insufficient evidence to provide a reliable answer."

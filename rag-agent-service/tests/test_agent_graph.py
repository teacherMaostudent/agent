from app.agent.graph import AgentGraph
from app.agent.models import AgentAction, AgentDecision
from app.domain.models import Evidence
from app.tools.registry import ToolRegistry


class SequenceDecisionEngine:
    def __init__(self, decisions: list[AgentDecision]) -> None:
        self.decisions = decisions

    def decide(self, state, tool_registry):
        return self.decisions.pop(0)


class FakeRepository:
    def regulation_chunks(self):
        return []

    def get_document(self, document_id):
        return None


class FakeRetriever:
    def search(self, query, chunks, top_k):
        return [
            Evidence(
                source_id="reg-1",
                source_type="regulation",
                text="Audit records must be attributable and retained.",
                score=0.93,
            )
        ]


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
            AgentDecision(action=AgentAction.ANSWER, final_answer="Audit records must be retained [reg-1]."),
        ]
    )
    graph = AgentGraph(engine, FakeRetriever(), FakeRepository(), ToolRegistry())

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
    graph = AgentGraph(engine, FakeRetriever(), FakeRepository(), ToolRegistry())

    result = graph.run(initial_state(max_steps=2), "thread-limit")

    assert result.termination_reason == "MAX_STEPS"
    assert result.steps == 2


def test_answer_without_evidence_is_blocked() -> None:
    engine = SequenceDecisionEngine(
        [AgentDecision(action=AgentAction.ANSWER, final_answer="An unsupported confident answer")]
    )
    graph = AgentGraph(engine, FakeRetriever(), FakeRepository(), ToolRegistry())

    result = graph.run(initial_state(), "thread-no-evidence")

    assert result.answer == "Insufficient evidence to provide a reliable answer."

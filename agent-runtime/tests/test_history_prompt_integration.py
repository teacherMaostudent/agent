import json

from app.tools.registry import ToolRegistry

from agent_runtime_service.agent.decision_engine import GatewayDecisionEngine
from agent_runtime_service.agent.models import AgentAction
from agent_runtime_service.runtime.planner import GatewaySemanticAnalyzer


class CapturingGateway:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete_json(self, model, system_prompt, user_prompt, execution_headers=None):
        self.calls.append(json.loads(user_prompt))
        return self.response

    def last_cost_usd(self):
        return 0.0


def base_state():
    return {
        "task": "What about that requirement?",
        "metadata": {},
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "permissions": [],
        "request_id": "request-a",
        "max_steps": 4,
        "conversation_history": [
            {"role": "user", "content": "We were discussing audit retention."}
        ],
        "user_context": {"department": "quality"},
        "context_status": {"rag_status": "not_requested", "degraded": False},
        "agent_snapshot": {},
        "budget": {"max_attempts": 4, "attempts_used": 0},
    }


def test_history_enters_semantic_planning_prompt() -> None:
    gateway = CapturingGateway(
        {
            "intent": {"name": "knowledge_query", "confidence": 0.9},
            "entities": [],
            "source_plan": {},
        }
    )
    analyzer = GatewaySemanticAnalyzer(gateway, "logical-model")

    analyzer.analyze(base_state())

    assert gateway.calls[0]["conversation_history"][0]["content"].startswith("We were")
    assert gateway.calls[0]["user_context"]["department"] == "quality"


def test_history_enters_agent_decision_prompt() -> None:
    gateway = CapturingGateway({"action": "ANSWER", "final_answer": "Retention is required."})
    engine = GatewayDecisionEngine(gateway, "logical-model")

    decision = engine.decide(base_state(), ToolRegistry())

    assert decision.action == AgentAction.ANSWER
    assert gateway.calls[0]["conversation_history"][0]["content"].startswith("We were")
    assert gateway.calls[0]["context_status"]["rag_status"] == "not_requested"

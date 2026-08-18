from platform_sdk.contracts.context import ContextPackage
from platform_sdk.contracts.models import Evidence
from platform_sdk.tools.registry import ToolRegistry

from agent_runtime_service.agent.graph import AgentGraph
from agent_runtime_service.agent.models import AgentAction, AgentDecision
from agent_runtime_service.runtime.mailbox import ClaimedRunMailboxItem, RunMailboxInputType
from agent_runtime_service.runtime.models import IntentResult, SourcePlan, UserInputResume
from agent_runtime_service.runtime.planner import RuntimePlanner
from agent_runtime_service.runtime.runtime_context import RuntimeContext


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


def runtime_context() -> RuntimeContext:
    """为 Graph 测试显式提供受限能力视图，避免测试绕过生产运行时边界。"""
    return RuntimeContext(context=FakeContextClient(), tools=ToolRegistry())


def test_agent_retrieves_then_answers() -> None:
    engine = SequenceDecisionEngine(
        [
            AgentDecision(action=AgentAction.RETRIEVE, query="audit record retention"),
            AgentDecision(
                action=AgentAction.ANSWER, final_answer="Audit records must be retained [reg-1]."
            ),
        ]
    )
    graph = AgentGraph(engine, runtime_context())

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
    graph = AgentGraph(engine, runtime_context())

    result = graph.run(initial_state(max_steps=2), "thread-limit")

    assert result.termination_reason == "MAX_STEPS"
    assert result.steps == 2


def test_answer_without_evidence_is_blocked() -> None:
    engine = SequenceDecisionEngine(
        [AgentDecision(action=AgentAction.ANSWER, final_answer="An unsupported confident answer")]
    )
    graph = AgentGraph(engine, runtime_context())

    result = graph.run(initial_state(), "thread-no-evidence")

    assert result.answer == "Insufficient evidence to provide a reliable answer."


def test_mailbox_input_forces_context_reload_before_next_decision() -> None:
    """外部输入只能在安全点被领取，并经 Context 重装/重新规划后才交给模型决策。"""

    class Mailbox:
        def __init__(self) -> None:
            self.claimed = False
            self.acknowledged = False

        def claim_mailbox_input(self, tenant_id, run_id):
            del tenant_id, run_id
            if self.claimed:
                return None
            self.claimed = True
            return ClaimedRunMailboxItem("mbx-1", RunMailboxInputType.STEERING, "lease-1")

        def acknowledge_mailbox_input(self, message_id, lease_token):
            self.acknowledged = message_id == "mbx-1" and lease_token == "lease-1"
            return self.acknowledged

    mailbox = Mailbox()
    graph = AgentGraph(
        SequenceDecisionEngine([AgentDecision(action=AgentAction.ANSWER, final_answer="Safe answer")]),
        runtime_context(),
        mailbox=mailbox,
    )

    graph.run(initial_state(), "thread-mailbox")
    assert mailbox.claimed and mailbox.acknowledged


def test_low_confidence_plan_interrupts_as_waiting_input() -> None:
    """澄清不再伪装成已完成回答，而是保留同一 LangGraph 检查点等待用户输入。"""

    class LowConfidenceAnalyzer:
        uses_llm = False

        def analyze(self, state):
            del state
            return IntentResult(name="unknown", confidence=0.2), [], SourcePlan()

    graph = AgentGraph(
        SequenceDecisionEngine([]),
        runtime_context(),
        planner=RuntimePlanner(LowConfidenceAnalyzer()),
    )

    result = graph.run(initial_state(), "thread-wait-input")

    assert result.status == "WAITING_INPUT"
    assert result.termination_reason == "USER_INPUT_REQUIRED"


def test_waiting_input_resumes_same_graph_checkpoint_after_mailbox_lease() -> None:
    """澄清输入恢复原线程，确认租约后重新规划，而不是用新 Run 跳过原快照。"""

    class Analyzer:
        uses_llm = False

        def __init__(self):
            self.calls = 0

        def analyze(self, state):
            del state
            self.calls += 1
            confidence = 0.2 if self.calls == 1 else 0.9
            return IntentResult(name="general_question", confidence=confidence), [], SourcePlan()

    class Mailbox:
        def acknowledge_mailbox_input(self, message_id, lease_token):
            return message_id == "mbx-1" and lease_token == "lease-1"

        def claim_mailbox_input(self, tenant_id, run_id):
            del tenant_id, run_id
            return None

    graph = AgentGraph(
        SequenceDecisionEngine([AgentDecision(action=AgentAction.ANSWER, final_answer="Safe answer")]),
        runtime_context(),
        planner=RuntimePlanner(Analyzer()),
        mailbox=Mailbox(),
    )
    assert graph.run(initial_state(), "thread-resume-input").status == "WAITING_INPUT"

    resumed = graph.resume(
        "thread-resume-input", UserInputResume(message_id="mbx-1", lease_token="lease-1"), max_steps=5
    )

    assert resumed.status == "COMPLETED"

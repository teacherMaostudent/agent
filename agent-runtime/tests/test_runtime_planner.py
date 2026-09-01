from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from langgraph.checkpoint.sqlite import SqliteSaver

from agent_runtime_service.agent.decision_engine import OfflineDecisionEngine
from agent_runtime_service.agent.graph import AgentGraph
from agent_runtime_service.agent.models import AgentAction, AgentDecision
from agent_runtime_service.runtime.budget import BudgetGuard
from agent_runtime_service.runtime.models import ApprovalResume, RuntimeBudget
from agent_runtime_service.runtime.planner import HeuristicSemanticAnalyzer, RuntimePlanner
from agent_runtime_service.runtime.runtime_context import RuntimeContext
from agent_runtime_service.runtime.session_events import RuntimeEventType
from agent_runtime_service.runtime.snapshot_compiler import compile_snapshot
from agent_runtime_service.runtime.tool_execution import SideEffectBarrier


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


def runtime_context(*, tools=None) -> RuntimeContext:
    """构造测试 RuntimeContext，使 AgentGraph 不再接收分散服务客户端。"""
    return RuntimeContext(context=EmptyContext(), tools=tools or ApprovalTool())


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


def test_offline_engine_terminates_after_empty_retrieval() -> None:
    """离线模式即使没有命中证据也应显式回答未知，不能无限重复检索。"""
    current = state("Question without indexed evidence")
    current["evidence"] = []
    current["budget"]["retrieval_rounds"] = 1

    decision = OfflineDecisionEngine().decide(current, tool_registry=None)  # type: ignore[arg-type]

    assert decision.action == AgentAction.ANSWER
    assert "No relevant evidence" in decision.final_answer


def test_offline_engine_executes_only_published_explicit_scan() -> None:
    """本地桌面演示可以验证真实 Tool Gateway 链路，但不能把任意任务变成文件扫描。"""
    current = state("使用 controlled_scan 扫描源码中的 TODO")
    current["compiled_plan"] = {"tools": [{"tool_name": "controlled_scan", "version": "1.0.0"}]}

    decision = OfflineDecisionEngine().decide(current, tool_registry=None)  # type: ignore[arg-type]

    assert decision.action == AgentAction.TOOL
    assert decision.tool_name == "controlled_scan"
    assert decision.tool_arguments == {
        "scope": "workspace",
        "pattern": "TODO",
        "regex": False,
        "glob": "**/*",
    }


def test_offline_engine_does_not_disguise_scan_failure_as_empty_result() -> None:
    """工具失败必须在最终答案中可见，不能与“没有命中”混淆。"""
    current = state("使用 controlled_scan 扫描源码中的 TODO")
    current["compiled_plan"] = {"tools": [{"tool_name": "controlled_scan", "version": "1.0.0"}]}
    current["observations"] = [
        {
            "type": "tool",
            "tool": "controlled_scan",
            "success": False,
            "error": "tool upstream transport failed",
        }
    ]

    decision = OfflineDecisionEngine().decide(current, tool_registry=None)  # type: ignore[arg-type]

    assert decision.action == AgentAction.ANSWER
    assert "did not complete" in decision.final_answer


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


def test_planner_uses_intent_catalog_frozen_in_compiled_snapshot() -> None:
    """不同业务 Agent 的意图规则应随发布计划执行，不能依赖 Runtime 的静态默认词表。"""
    planner = RuntimePlanner(HeuristicSemanticAnalyzer())
    current = state("请为客户安排现场勘察")
    current["compiled_plan"] = {
        "intent_catalog_version": "field-service/v1",
        "intent_catalog": {
            "version": "field-service/v1",
            "definitions": [
                {
                    "name": "site_visit",
                    "domain": "field-service",
                    "action": "schedule",
                    "examples": ["现场勘察", "现场预约"],
                    "required_entities": ["business_id"],
                }
            ],
        },
    }

    analysis = planner.analyze(current)

    assert analysis["intent"]["name"] == "site_visit"


def test_planner_projects_frozen_agent_topology_instead_of_hardcoding_single() -> None:
    """审计中的 Topology 必须与发布委派/独立责任主体一致。"""
    planner = RuntimePlanner(HeuristicSemanticAnalyzer())
    current = state("调查跨部门问题")
    current["compiled_plan"] = {"subagents": [{"agent_id": "worker"}]}
    current.update(planner.analyze(current))
    assert planner.build_plan(current).topology == "sub_agent"

    current["compiled_plan"] = {
        "capability_providers": [
            {"kind": "agent", "requires_independent_authority": True}
        ]
    }
    assert planner.build_plan(current).topology == "multi_agent"


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
        runtime_context(),
        budget_guard=BudgetGuard(0.01, 0.001),
    )

    result = graph.run(state("General question", budget=budget), "thread-limit")

    assert result.status == "LIMIT_EXCEEDED"
    assert result.termination_reason == "MAX_LLM_CALLS"
    assert "模型调用次数" in result.answer
    assert result.budget["max_llm_calls"] == 0
    assert len(engine.decisions) == 1


def test_published_workflow_blocks_model_action_not_declared_by_graph() -> None:
    """模型建议检索不能绕过发布 Graph 中仅允许回答的受限迁移。"""
    published = {
        "schema_version": "1.0",
        "tenant_id": "tenant-a",
        "agent_id": "agent-a",
        "spec": {
            "graph": {
                "graph_id": "answer-only",
                "entrypoint": "decide",
                "terminal_nodes": ["answer"],
                "nodes": [
                    {"node_id": "decide", "kind": "decision"},
                    {"node_id": "answer", "kind": "answer"},
                ],
                "edges": [{"from_node": "decide", "to_node": "answer"}],
            },
            "prompt": {"system_template": "Answer {{task}}", "variables": ["task"]},
            "model_policy": {
                "default_route": "primary",
                "routes": [{"route_name": "primary", "models": ["model-a"]}],
            },
        },
    }
    current = state("Question")
    current["agent_snapshot"] = published
    current["compiled_plan"] = compile_snapshot(
        published, tenant_id="tenant-a", agent_id="agent-a", fallback_model="model-a"
    ).model_dump(mode="json")
    engine = SequenceEngine([AgentDecision(action=AgentAction.RETRIEVE, query="forbidden")])
    graph = AgentGraph(engine, runtime_context())

    result = graph.run(current, "thread-workflow-policy")

    assert result.status == "LIMIT_EXCEEDED"
    assert result.termination_reason == "WORKFLOW_ACTION_FORBIDDEN"


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
        runtime_context(),
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
        runtime_context(tools=ApprovalTool()),
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
        runtime_context(tools=ApprovalTool()),
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


def test_tool_session_facts_distinguish_intent_dispatch_commit_and_result() -> None:
    """副作用恢复必须知道调用处于哪个阶段，不能只记录一个模糊的“工具已调用”。"""

    class ImmediateTool:
        """模拟立即返回已提交结果的 Tool Gateway 客户端。"""

        def manifests(self, permissions, **kwargs):
            """返回已发布的最小工具目录投影。"""
            del permissions, kwargs
            return [{"name": "payments.refund", "risk": "read_only"}]

        def execute(self, name, arguments, context):
            """模拟 Gateway 已提交的幂等工具结果。"""
            del name, arguments, context
            return {"status": "accepted", "refund_id": "refund-001"}

    class CapturingEngine(SequenceEngine):
        """记录第二次 Decision 所见 Evidence，证明图节点顺序没有被后续改动绕过。"""

        def __init__(self, decisions):
            super().__init__(decisions)
            self.evidence_before_final_decision: list[dict] = []

        def decide(self, current_state, tool_registry):
            if len(self.decisions) == 1:
                self.evidence_before_final_decision = list(current_state.get("evidence", []))
            return super().decide(current_state, tool_registry)

    facts: list[RuntimeEventType] = []
    engine = CapturingEngine(
        [
            AgentDecision(action=AgentAction.TOOL, tool_name="payments.refund"),
            AgentDecision(action=AgentAction.ANSWER, final_answer="Refund accepted."),
        ]
    )
    graph = AgentGraph(
        engine,
        runtime_context(tools=ImmediateTool()),
        session_event_recorder=lambda _state, event_type, _metadata, _message: facts.append(event_type),
    )

    result = graph.run(state("Refund order ORD-123"), "tool-facts")

    assert result.status == "COMPLETED"
    assert result.tool_evidence[-1]["status"] == "STORED"
    assert result.evidence[-1]["source_type"] == "tool_observation"
    assert engine.evidence_before_final_decision[-1]["evidence_id"] == result.evidence[-1]["evidence_id"]
    assert [
        event
        for event in facts
        if event
        in {
            RuntimeEventType.TOOL_INTENT_RECORDED,
            RuntimeEventType.TOOL_DISPATCHED,
            RuntimeEventType.TOOL_COMMITTED,
            RuntimeEventType.TOOL_RESULT,
            RuntimeEventType.TOOL_EVIDENCE_STORED,
        }
    ] == [
        RuntimeEventType.TOOL_INTENT_RECORDED,
        RuntimeEventType.TOOL_DISPATCHED,
        RuntimeEventType.TOOL_COMMITTED,
        RuntimeEventType.TOOL_RESULT,
        RuntimeEventType.TOOL_EVIDENCE_STORED,
    ]


def test_pending_steering_defers_side_effect_before_tool_gateway_dispatch() -> None:
    """新 Steering 到达后，旧模型决策不能先提交写操作，Graph 必须回到 Context/Planner。"""

    class WriteTool:
        """记录真实调用次数，用于证明屏障在 Gateway 前生效。"""

        def __init__(self) -> None:
            """初始化零调用计数。"""
            self.calls = 0

        def manifests(self, permissions, **kwargs):
            """返回已发布的高风险写工具目录。"""
            del permissions, kwargs
            return [{"name": "payments.refund", "risk": "write_high_risk"}]

        def execute(self, name, arguments, context):
            """仅在副作用屏障放行时递增计数。"""
            del name, arguments, context
            self.calls += 1
            return {"status": "accepted"}

    current = state("Execute refund for ORD-123")
    current["compiled_plan"] = {
        "tools": [
            {
                "tool_name": "payments.refund",
                "version": "1.0.0",
                "risk": "write_high_risk",
                "side_effect": True,
                "idempotent": True,
            }
        ]
    }
    class PendingSteering:
        """模拟在模型决定工具后、Gateway 调用前到达的 Steering 事实。"""

        def has_pending_replan_input(self, tenant_id, run_id):
            """返回待处理输入；不领取消息，保持屏障的只读边界。"""
            del tenant_id, run_id
            return True

    tool = WriteTool()
    events: list[RuntimeEventType] = []
    graph = AgentGraph(
        SequenceEngine([
            AgentDecision(action=AgentAction.TOOL, tool_name="payments.refund"),
            AgentDecision(action=AgentAction.ANSWER, final_answer="updated safely"),
        ]),
        runtime_context(tools=tool),
        side_effect_barrier=SideEffectBarrier(inbox=PendingSteering()),
        session_event_recorder=lambda _state, event, _metadata, _message: events.append(event),
    )

    result = graph.run(current, "side-effect-barrier")

    assert result.status == "COMPLETED"
    assert tool.calls == 0
    assert RuntimeEventType.TOOL_DISPATCH_DEFERRED in events

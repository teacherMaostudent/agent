"""LangGraph implementation of the bounded Runtime action loop.

LangGraph persists graph control flow; this module adds platform invariants
around published snapshots, approvals, budgets and untrusted observation data.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from inspect import signature
from typing import Any

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from opentelemetry import trace
from platform_sdk.contracts.context import ContextAssembleRequest
from platform_sdk.contracts.rag import RagSearchRequest
from platform_sdk.contracts.workflow import evaluate_workflow_condition
from platform_sdk.security import bound_untrusted
from platform_sdk.tools.registry import ToolContext, ToolRegistry

from agent_runtime_service.agent.decision_engine import DecisionEngine
from agent_runtime_service.agent.models import (
    AgentAction,
    AgentDecision,
    AgentRunResult,
    AgentState,
)
from agent_runtime_service.runtime.budget import BudgetGuard
from agent_runtime_service.runtime.models import (
    ApprovalResume,
    RouteType,
    RuntimeBudget,
    RuntimeCancelled,
    RuntimeLimitExceeded,
)
from agent_runtime_service.runtime.planner import HeuristicSemanticAnalyzer, RuntimePlanner
from agent_runtime_service.runtime.snapshot_compiler import validate_final_output


class AgentGraph:
    """Bounded decide -> retrieve/tool -> observe loop with a deterministic safety exit.

    LangGraph provides the durable graph mechanics; this class owns platform
    semantics: published-plan enforcement, Context assembly, budget checks,
    approval interrupts and sanitised observations passed back to the model.
    """

    def __init__(
        self,
        decision_engine: DecisionEngine,
        tool_registry: ToolRegistry | None = None,
        *,
        context_client,
        rag_client=None,
        planner: RuntimePlanner | None = None,
        budget_guard: BudgetGuard | None = None,
        checkpointer=None,
        cancellation_checker: Callable[[str, str], bool] | None = None,
    ) -> None:
        """组装受控执行图。

        ``context_client`` 是会话与 ACL 的唯一所有者，``rag_client`` 仅能经稳定
        契约读取证据；二者均不得让 Runtime 直接接触其内部存储。可选取消检查器在
        每个有副作用节点前执行，使 API 取消能够在长任务中尽快生效。
        """
        self.decision_engine = decision_engine
        self.context_client = context_client
        # Evidence is read only through the published RAG contract. Context
        # owns conversation memory and its ACL boundary; Runtime owns neither.
        self.rag_client = rag_client
        self._context_accepts_execution_headers = (
            "execution_headers" in signature(context_client.assemble).parameters
        )
        self.tool_registry = tool_registry or ToolRegistry()
        self.planner = planner or RuntimePlanner(HeuristicSemanticAnalyzer())
        self.budget_guard = budget_guard or BudgetGuard(0.0, 0.0)
        self.cancellation_checker = cancellation_checker
        self.graph = self._build().compile(checkpointer=checkpointer or InMemorySaver())

    def _build(self):
        """声明不可绕过的状态机拓扑。

        规划先于模型决策；检索和工具调用前分别经过预算守卫，审批中断恢复后回到
        同一持久化线程。这里不按模型输出动态加边，避免模型取得流程控制权。
        """
        # The plan phase runs before free-form decisioning.  It constrains
        # reachable knowledge sources and tools to the published snapshot.
        graph = StateGraph(AgentState)
        graph.add_node("load_memory", self._load_memory)
        graph.add_node("analyze", self._analyze)
        graph.add_node("build_plan", self._build_plan)
        graph.add_node("planned_retrieval_guard", self._retrieval_guard)
        graph.add_node("planned_retrieve", self._planned_retrieve)
        graph.add_node("decide", self._decide)
        graph.add_node("retrieval_guard", self._retrieval_guard)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("tool_guard", self._tool_guard)
        graph.add_node("tool", self._tool)
        graph.add_node("clarify", self._clarify)
        graph.add_node("finalize", self._finalize)
        graph.add_node("safety", self._safety)
        graph.add_edge(START, "load_memory")
        graph.add_edge("load_memory", "analyze")
        graph.add_edge("analyze", "build_plan")
        graph.add_conditional_edges(
            "build_plan",
            self._plan_route,
            {
                "clarify": "clarify",
                "rag": "planned_retrieval_guard",
                "agent": "decide",
            },
        )
        graph.add_edge("planned_retrieval_guard", "planned_retrieve")
        graph.add_conditional_edges(
            "planned_retrieve",
            self._after_retrieval,
            {"decide": "decide", "clarify": "clarify"},
        )
        graph.add_conditional_edges(
            "decide",
            self._route,
            {
                "retrieve": "retrieval_guard",
                "tool": "tool_guard",
                "answer": "finalize",
                "limit": "finalize",
            },
        )
        graph.add_edge("retrieval_guard", "retrieve")
        graph.add_conditional_edges(
            "retrieve",
            self._after_retrieval,
            {"decide": "decide", "clarify": "clarify"},
        )
        graph.add_edge("tool_guard", "tool")
        graph.add_conditional_edges(
            "tool",
            self._after_tool,
            {"continue": "decide", "finish": "safety"},
        )
        graph.add_edge("clarify", "safety")
        graph.add_edge("finalize", "safety")
        graph.add_edge("safety", END)
        return graph

    def run(self, initial: AgentState, thread_id: str) -> AgentRunResult:
        """从初始状态执行一次可恢复运行并转换内部异常为稳定结果。

        ``thread_id`` 只用于 LangGraph 检查点，不授予业务权限；租户仍来自状态。
        限额和取消属于预期终止，不能向调用者泄露框架异常或误报成成功。
        """
        config = self._config(thread_id, initial["max_steps"])
        try:
            with trace.get_tracer(__name__).start_as_current_span("agent.graph.run") as span:
                span.set_attribute("agent.thread_id", thread_id)
                span.set_attribute("tenant.id", initial["tenant_id"])
                result = self.graph.invoke(initial, config)
            return self._result(result)
        except RuntimeLimitExceeded as exc:
            return self._limited_result(initial, exc)
        except RuntimeCancelled:
            return self._cancelled_result(initial)

    def resume(
        self,
        thread_id: str,
        approval: ApprovalResume,
        *,
        max_steps: int,
    ) -> AgentRunResult:
        """向已中断的审批节点注入人工决定并继续原线程。

        只接受 ``ApprovalResume`` 结构化载荷，避免将任意 API body 写入图状态；
        检查点决定恢复位置，因此不得重新提供或重放初始请求。
        """
        try:
            result = self.graph.invoke(
                Command(resume=approval.model_dump(mode="json")),
                self._config(thread_id, max_steps),
            )
            return self._result(result)
        except RuntimeLimitExceeded as exc:
            return self._limited_result({}, exc)
        except RuntimeCancelled:
            return self._cancelled_result({})

    @staticmethod
    def _config(thread_id: str, max_steps: int) -> dict[str, Any]:
        """生成检查点标识和防死循环递归上限。

        上限高于业务步数以容纳守卫、审批和收尾节点；实际业务步数仍由预算守卫
        限制，不能借此放宽 ``max_steps``。
        """
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": max(30, max_steps * 5 + 15),
        }

    def _analyze(self, state: AgentState) -> dict:
        """执行意图、实体和来源分析，并为可选 LLM 分析做成本预留/对账。"""
        self._ensure_active(state)
        budget = self._budget(state)
        if self.planner.analyzer.uses_llm:
            budget = self.budget_guard.reserve_llm(budget)
        working = {**state, "budget": budget.model_dump(mode="json")}
        analysis = self.planner.analyze(working)
        if self.planner.analyzer.uses_llm:
            cost_reader = getattr(self.planner.analyzer, "last_cost_usd", None)
            actual_cost = cost_reader() if cost_reader else None
            budget = self.budget_guard.reconcile_cost(
                budget,
                reserved_usd=self.budget_guard.llm_call_reservation_usd,
                actual_usd=actual_cost,
            )
        return {
            **analysis,
            "budget": budget.model_dump(mode="json"),
            "execution_trace": self._trace(
                state, "analyze", {"intent": analysis["intent"]["name"]}
            ),
        }

    def _load_memory(self, state: AgentState) -> dict:
        """从 Context 服务读取已授权历史，但不在此阶段触发 RAG。

        当前用户的同一问题会从历史中排除，防止 prompt 重复；Context 的降级状态
        原样保留到运行状态，供后续规划与审计判断。
        """
        self._ensure_active(state)
        request = ContextAssembleRequest(
            session_id=state.get("session_id", state["request_id"]),
            query=state["task"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            metadata=state.get("metadata", {}),
            include_rag=False,
            rag_required=False,
        )
        if self._context_accepts_execution_headers:
            package = self.context_client.assemble(
                request, execution_headers=self._execution_headers(state)
            )
        else:
            package = self.context_client.assemble(request)
        history = [
            item.model_dump(mode="json")
            for item in package.recent_messages
            if not (item.role == "user" and item.content == state["task"])
        ]
        return {
            "conversation_history": history,
            "user_context": package.user_context,
            "context_status": {
                "rag_status": package.rag_status,
                "degraded": package.degraded,
                "degrade_reason": package.degrade_reason,
                "budget_report": (
                    package.budget_report.model_dump(mode="json") if package.budget_report else None
                ),
            },
            "execution_trace": self._trace(state, "load_memory", {"message_count": len(history)}),
        }

    def _build_plan(self, state: AgentState) -> dict:
        """把分析结果编译为当前运行唯一可用的路由、SLA 与检索策略。"""
        self._ensure_active(state)
        plan = self.planner.build_plan(state)
        workflow = state.get("compiled_plan", {}).get("workflow_policy", {})
        return {
            "execution_plan": plan.model_dump(mode="json"),
            # Cursor is a published Graph node, not a model-selected string.
            # Every later action advances it through the compiled adjacency map.
            "workflow_cursor": workflow.get("entrypoint", ""),
            "execution_trace": self._trace(
                state,
                "build_plan",
                {"route": plan.route.route.value, "complexity": plan.complexity.score},
            ),
        }

    @staticmethod
    def _plan_route(state: AgentState) -> str:
        """从发布工作流入口选择固定 Runtime 节点，不让 Planner 改写业务图。"""
        workflow = state.get("compiled_plan", {}).get("workflow_policy", {})
        roles = workflow.get("node_roles", {})
        entry_role = roles.get(workflow.get("entrypoint"))
        if entry_role == "retrieval":
            return "rag"
        if entry_role == "clarify":
            return "clarify"
        if entry_role == "decision":
            return "agent"
        if workflow and not workflow.get("local_development_only"):
            raise RuntimeLimitExceeded("WORKFLOW_ENTRY_INVALID", "Published workflow entry is invalid.")
        route = state["execution_plan"]["route"]["route"]
        if route == RouteType.CLARIFY:
            return "clarify"
        if route == RouteType.RAG:
            return "rag"
        return "agent"

    def _decide(self, state: AgentState) -> dict:
        """请求一次下一动作，并在模型调用前后严格管理预算。

        决策引擎只能返回动作，不能直接执行工具或改变图边；网关实际费用会覆盖预留
        值，避免由于估算偏差长期低报成本。
        """
        self._ensure_active(state)
        budget = self.budget_guard.count_step(self._budget(state))
        if getattr(self.decision_engine, "uses_llm", False):
            budget = self.budget_guard.reserve_llm(budget)
        working = {**state, "budget": budget.model_dump(mode="json")}
        with trace.get_tracer(__name__).start_as_current_span("agent.decide"):
            decision = self.decision_engine.decide(working, self.tool_registry)
        next_node = self._workflow_next_node(state, decision.action)
        if getattr(self.decision_engine, "uses_llm", False):
            cost_reader = getattr(self.decision_engine, "last_cost_usd", None)
            actual_cost = cost_reader() if cost_reader else None
            budget = self.budget_guard.reconcile_cost(
                budget,
                reserved_usd=self.budget_guard.llm_call_reservation_usd,
                actual_usd=actual_cost,
            )
        return {
            "decision": decision.model_dump(mode="json"),
            "workflow_cursor": next_node or state.get("workflow_cursor", ""),
            "step_count": state.get("step_count", 0) + 1,
            "budget": budget.model_dump(mode="json"),
            "execution_trace": self._trace(
                state,
                "decide",
                {"action": decision.action.value},
            ),
        }

    def _route(self, state: AgentState) -> str:
        """依据已验证的动作选择后继节点，超过步骤上限时优先终止。"""
        if state.get("step_count", 0) >= state["max_steps"]:
            return "limit"
        action = AgentDecision.model_validate(state["decision"]).action
        return {
            AgentAction.RETRIEVE: "retrieve",
            AgentAction.TOOL: "tool",
            AgentAction.ANSWER: "answer",
        }[action]

    def _retrieve(self, state: AgentState) -> dict:
        """执行模型明确请求的下一轮检索，查询仅取已校验的决策字段。"""
        decision = AgentDecision.model_validate(state["decision"])
        return self._do_retrieve(state, decision.query, "retrieve")

    def _planned_retrieve(self, state: AgentState) -> dict:
        """执行计划强制要求的首轮检索，避免模型跳过发布时约束的证据步骤。"""
        return self._do_retrieve(state, state["task"], "planned_retrieve")

    def _retrieval_guard(self, state: AgentState) -> dict:
        """在检索前消耗轮次预算；达到策略上限立即失败而非静默削弱证据要求。"""
        self._ensure_active(state)
        current = self._budget(state)
        policy = state.get("execution_plan", {}).get("retrieval_policy", {})
        if current.retrieval_rounds >= int(policy.get("max_rounds", current.max_retrieval_rounds)):
            raise RuntimeLimitExceeded(
                "RETRIEVAL_PROFILE_LIMIT",
                "The effective retrieval profile has exhausted its round budget.",
            )
        budget = self.budget_guard.reserve_retrieval(current)
        return {"budget": budget.model_dump(mode="json")}

    def _do_retrieve(self, state: AgentState, query: str, node_name: str) -> dict:
        """组装上下文并获取受发布知识绑定限制的证据。

        可选 RAG 故障仅在策略允许且证据非必需时降级为 memory-only；证据必需的
        发布版本必须失败关闭。历史、证据和降级原因都会写入审计状态。
        """
        self._ensure_active(state)
        compiled = state.get("compiled_plan", {})
        knowledge = compiled.get("knowledge", [])
        policy = state.get("execution_plan", {}).get("retrieval_policy", {})
        retrieval_top_k = int(
            policy.get("evidence_top_k")
            or max((int(item.get("top_k", 8)) for item in knowledge), default=8)
        )
        with trace.get_tracer(__name__).start_as_current_span("rag.retrieve") as span:
            no_rag = policy.get("profile") == "NO_RAG"
            use_direct_rag = self.rag_client is not None and not no_rag
            context_request = ContextAssembleRequest(
                session_id=state.get("session_id", state["request_id"]),
                query=query,
                tenant_id=state["tenant_id"],
                user_id=state["user_id"],
                document_id=state.get("document_id"),
                content=state.get("content"),
                metadata={
                    **state.get("metadata", {}),
                    "runtime_source_plan": state.get("source_plan", {}),
                    "published_knowledge": knowledge,
                    "effective_retrieval_policy": policy,
                },
                top_k=retrieval_top_k,
                # Context owns memory ranking.  When a remote RAG client is
                # configured, Runtime obtains evidence directly so its only
                # dependency on RAG is the published HTTP contract.
                include_rag=not use_direct_rag and not no_rag,
                rag_required=False if use_direct_rag or no_rag else self._rag_required(state),
            )
            if self._context_accepts_execution_headers:
                package = self.context_client.assemble(
                    context_request,
                    execution_headers=self._execution_headers(state),
                )
            else:
                package = self.context_client.assemble(context_request)
            if use_direct_rag:
                try:
                    rag_response = self.rag_client.search(
                        RagSearchRequest(
                            query=query,
                            tenant_id=state["tenant_id"],
                            user_id=state["user_id"],
                            document_id=state.get("document_id"),
                            content=state.get("content"),
                            metadata=context_request.metadata,
                            top_k=retrieval_top_k,
                        )
                    )
                    evidence_items = rag_response.evidence
                    rag_status = "available"
                    rag_degraded = False
                    rag_degrade_reason = None
                except httpx.HTTPError:
                    # A release may explicitly allow memory-only operation.
                    # Required-evidence tasks fail closed instead of allowing a
                    # fluent answer to stand in for an unverified fact.
                    if self._rag_required(state):
                        raise
                    evidence_items = []
                    rag_status = "memory_only"
                    rag_degraded = True
                    rag_degrade_reason = "rag_unavailable_memory_only"
            else:
                evidence_items = package.knowledge_evidence
                rag_status = package.rag_status
                rag_degraded = False
                rag_degrade_reason = None
            span.set_attribute("rag.retrieved_documents", len(evidence_items))
            span.set_attribute("context.estimated_tokens", package.estimated_tokens)
        evidence = [item.model_dump(mode="json") for item in evidence_items]
        history = [
            item.model_dump(mode="json")
            for item in package.recent_messages
            if not (item.role == "user" and item.content == state["task"])
        ]
        observation = {
            "type": "retrieval",
            "query": query,
            "result_count": len(evidence),
            "context_truncated": package.truncated,
            "context_degraded": package.degraded or rag_degraded,
            "rag_status": rag_status,
            "retrieval_profile": policy.get("profile", "STANDARD"),
        }
        return {
            "evidence": [*state.get("evidence", []), *evidence],
            "conversation_history": history,
            "user_context": package.user_context,
            "context_status": {
                "rag_status": rag_status,
                "degraded": package.degraded or rag_degraded,
                "degrade_reason": package.degrade_reason or rag_degrade_reason,
                "budget_report": (
                    package.budget_report.model_dump(mode="json") if package.budget_report else None
                ),
            },
            "workflow_cursor": self._workflow_after_side_effect(state, "retrieval"),
            "observations": [*state.get("observations", []), observation],
            "execution_trace": self._trace(
                state,
                node_name,
                {"result_count": len(evidence)},
            ),
        }

    @staticmethod
    def _rag_required(state: AgentState) -> bool:
        """按“有效策略→请求覆盖→发布绑定”优先级判定 RAG 是否必须成功。"""
        effective = state.get("execution_plan", {}).get("retrieval_policy", {})
        if "retrieval_required" in effective:
            return bool(effective["retrieval_required"])
        metadata = state.get("metadata", {})
        if "rag_required" in metadata:
            return bool(metadata["rag_required"])
        bindings = state.get("compiled_plan", {}).get("knowledge") or (
            state.get("agent_snapshot", {}).get("spec", {}).get("knowledge", [])
        )
        if not bindings:
            return True
        return any(
            bool(item.get("required", True)) and item.get("failure_mode", "fail") != "memory_only"
            for item in bindings
        )

    def _tool_guard(self, state: AgentState) -> dict:
        """在任何工具执行前预留一次工具费用，防止审批后绕过成本限制。"""
        self._ensure_active(state)
        budget = self.budget_guard.reserve_tool(self._budget(state))
        return {"budget": budget.model_dump(mode="json")}

    def _tool(self, state: AgentState) -> dict:
        """执行发布版本绑定的工具，并在重新提示模型前限制、脱敏不可信输出。

        工具待审批时图会持久化中断；拒绝直接终止，批准后只用一次性审批标识重试
        原调用。普通工具异常转为观察结果，供模型在剩余预算内处理。
        """
        self._ensure_active(state)
        decision = AgentDecision.model_validate(state["decision"])
        published_version = self._published_tool_version(state, decision.tool_name)
        approval_ids = state.get("metadata", {}).get("tool_approval_ids", {})
        approval_id = (
            str(approval_ids.get(decision.tool_name, "")) if isinstance(approval_ids, dict) else ""
        )
        context = self._tool_context(state, approval_id, published_version)
        try:
            result = self.tool_registry.execute(
                decision.tool_name, decision.tool_arguments, context
            )
            if isinstance(result, dict) and result.get("status") == "PENDING_APPROVAL":
                resume_value = interrupt(
                    {
                        "type": "tool_approval",
                        "run_id": state.get("run_id", ""),
                        "tool_name": decision.tool_name,
                        "tool_arguments": decision.tool_arguments,
                        "approval_id": result.get("approval_id", ""),
                    }
                )
                approval = ApprovalResume.model_validate(resume_value)
                if not approval.approved:
                    observation = {
                        "type": "tool",
                        "tool": decision.tool_name,
                        "success": False,
                        "status": "REJECTED",
                        "reason": approval.reason,
                    }
                    return {
                        "observations": [*state.get("observations", []), observation],
                        "final_answer": "The requested tool action was not approved.",
                        "termination_reason": "TOOL_REJECTED",
                        "execution_trace": self._trace(
                            state,
                            "tool_rejected",
                            {"tool": decision.tool_name},
                        ),
                    }
                approved_id = approval.approval_id or str(result.get("approval_id", ""))
                context = self._tool_context(state, approved_id, published_version)
                result = self.tool_registry.execute(
                    decision.tool_name,
                    decision.tool_arguments,
                    context,
                )
            observation = {
                "type": "tool",
                "tool": decision.tool_name,
                "success": True,
                "result": bound_untrusted(
                    result,
                    int(state.get("metadata", {}).get("tool_result_max_chars", 12_000)),
                ),
            }
        except GraphInterrupt:
            raise
        except Exception as exc:
            observation = {
                "type": "tool",
                "tool": decision.tool_name,
                "success": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        return {
            "observations": [*state.get("observations", []), observation],
            "workflow_cursor": self._workflow_after_side_effect(state, "tool"),
            "execution_trace": self._trace(
                state,
                "tool",
                {"tool": decision.tool_name, "success": observation["success"]},
            ),
        }

    @staticmethod
    def _after_tool(state: AgentState) -> str:
        """按发布图决定工具后的安全去向，人工拒绝仍优先终止。"""
        if state.get("termination_reason") == "TOOL_REJECTED":
            return "finish"
        cursor = state.get("workflow_cursor", "")
        roles = state.get("compiled_plan", {}).get("workflow_policy", {}).get("node_roles", {})
        if not roles:
            return "continue"
        if roles.get(cursor) == "clarify":
            return "finish"
        if roles.get(cursor) in {"decision", "answer"}:
            return "continue"
        raise RuntimeLimitExceeded("WORKFLOW_CURSOR_INVALID", "Tool successor is not executable.")

    @staticmethod
    def _after_retrieval(state: AgentState) -> str:
        """把检索后的发布节点映射到固定安全图；answer 仍须经模型给出受控答案。"""
        cursor = state.get("workflow_cursor", "")
        roles = state.get("compiled_plan", {}).get("workflow_policy", {}).get("node_roles", {})
        if not roles or roles.get(cursor) in {"decision", "answer"}:
            return "decide"
        if roles.get(cursor) == "clarify":
            return "clarify"
        raise RuntimeLimitExceeded("WORKFLOW_CURSOR_INVALID", "Retrieval successor is not executable.")

    @staticmethod
    def _tool_context(
        state: AgentState,
        approval_id: str,
        tool_version: str,
    ) -> ToolContext:
        """从运行状态构造最小工具权限上下文。

        工具版本和审批标识由发布快照与审批流提供，模型参数不能覆盖；剩余尝试数
        下传给 Tool Gateway，使下游也能拒绝超过执行预算的请求。
        """
        return ToolContext(
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            permissions=frozenset(state.get("permissions", [])),
            request_id=state["request_id"],
            approval_id=approval_id,
            trace_id=state.get("trace_id", ""),
            run_id=state.get("run_id", ""),
            session_id=state.get("session_id", ""),
            agent_id=state.get("agent_id", ""),
            agent_version=state.get("agent_version", ""),
            snapshot_id=state.get("snapshot_id", ""),
            deadline_at=state.get("deadline_at", ""),
            attempt_budget_remaining=AgentGraph._budget(state).remaining_attempts,
            tool_version=tool_version,
        )

    @staticmethod
    def _published_tool_version(state: AgentState, tool_name: str) -> str:
        """返回快照绑定的工具版本；未绑定工具必须显式拒绝执行。"""
        spec = state.get("agent_snapshot", {}).get("spec", {})
        if "tools" not in spec:
            return ""
        for binding in spec.get("tools", []):
            if binding.get("tool_name") == tool_name:
                return str(binding.get("version", ""))
        raise RuntimeLimitExceeded(
            "TOOL_NOT_PUBLISHED",
            f"Tool '{tool_name}' is not bound in the published Agent snapshot.",
        )

    def _clarify(self, state: AgentState) -> dict:
        """为低置信度意图生成确定性澄清结果，不调用模型以避免猜测性副作用。"""
        return {
            "final_answer": (
                "Please clarify the request so that its intent can be determined safely."
            ),
            "termination_reason": "NEEDS_CLARIFICATION",
            "execution_trace": self._trace(state, "clarify", {}),
        }

    def _finalize(self, state: AgentState) -> dict:
        """验证最终回答符合发布输出契约，并处理非回答的步数耗尽。"""
        self._ensure_active(state)
        with trace.get_tracer(__name__).start_as_current_span("agent.generate_final_answer"):
            decision = AgentDecision.model_validate(state["decision"])
            if (
                state.get("step_count", 0) >= state["max_steps"]
                and decision.action != AgentAction.ANSWER
            ):
                return {
                    "final_answer": "Unable to complete within the configured agent step budget.",
                    "termination_reason": "MAX_STEPS",
                }
            validate_final_output(state.get("compiled_plan", {}), decision.final_answer)
            return {
                "final_answer": decision.final_answer,
                "termination_reason": "ANSWERED",
            }

    def _safety(self, state: AgentState) -> dict:
        """实施回答前的最小证据规则并记录可审计安全状态。

        工具成功可构成行动结果；否则若策略禁止无证据回答，不能以流畅文案掩盖
        检索失败或证据不足。
        """
        with trace.get_tracer(__name__).start_as_current_span("answer.safety_check"):
            answer = state.get("final_answer", "").strip()
            if state.get("termination_reason") == "NEEDS_CLARIFICATION":
                return {
                    "final_answer": answer,
                    "safety_status": "CLARIFICATION_REQUIRED",
                }
            if state.get("termination_reason") == "TOOL_REJECTED":
                return {"final_answer": answer, "safety_status": "ACTION_REJECTED"}
            successful_tool = any(
                item.get("type") == "tool" and item.get("success")
                for item in state.get("observations", [])
            )
            policy = state.get("execution_plan", {}).get("retrieval_policy", {})
            minimum = int(policy.get("minimum_evidence_count", 0))
            evidence_count = len(state.get("evidence", []))
            if evidence_count < minimum and not successful_tool:
                if policy.get("allow_answer_without_evidence", True):
                    return {"final_answer": answer, "safety_status": "PASSED_NO_EVIDENCE_ALLOWED"}
                return {
                    "final_answer": "Insufficient evidence to provide a reliable answer.",
                    "safety_status": "BLOCKED_NO_EVIDENCE",
                }
            if not answer:
                answer = "No safe answer could be produced from the available evidence."
            return {"final_answer": answer, "safety_status": "PASSED"}

    def _result(self, result: dict[str, Any]) -> AgentRunResult:
        """将 LangGraph 内部状态压缩为公开 API 结果，并保留审批中断详情。"""
        interrupt_items = [item.value for item in result.get("__interrupt__", [])]
        if interrupt_items:
            return AgentRunResult(
                status="WAITING_APPROVAL",
                answer="",
                steps=result.get("step_count", 0),
                termination_reason="HUMAN_APPROVAL_REQUIRED",
                evidence=result.get("evidence", []),
                observations=result.get("observations", []),
                execution_plan=result.get("execution_plan", {}),
                budget=result.get("budget", {}),
                execution_trace=result.get("execution_trace", []),
                interrupts=interrupt_items,
            )
        return AgentRunResult(
            status="COMPLETED",
            answer=result.get("final_answer", ""),
            steps=result.get("step_count", 0),
            termination_reason=result.get("termination_reason", "ANSWERED"),
            evidence=result.get("evidence", []),
            observations=result.get("observations", []),
            execution_plan=result.get("execution_plan", {}),
            budget=result.get("budget", {}),
            execution_trace=result.get("execution_trace", []),
        )

    @staticmethod
    def _limited_result(state: dict[str, Any], error: RuntimeLimitExceeded) -> AgentRunResult:
        """将预算或发布边界错误标准化为无敏感细节的 ``LIMIT_EXCEEDED`` 响应。"""
        return AgentRunResult(
            status="LIMIT_EXCEEDED",
            answer="Unable to complete within the configured runtime limits.",
            steps=state.get("step_count", 0),
            termination_reason=error.code,
            evidence=state.get("evidence", []),
            observations=state.get("observations", []),
            execution_plan=state.get("execution_plan", {}),
            budget=state.get("budget", {}),
            execution_trace=state.get("execution_trace", []),
        )

    @staticmethod
    def _cancelled_result(state: dict[str, Any]) -> AgentRunResult:
        """将协作取消转为幂等终态，并保留已有执行痕迹供治理审计。"""
        return AgentRunResult(
            status="CANCELLED",
            answer="The run was cancelled.",
            steps=state.get("step_count", 0),
            termination_reason="CANCELLED",
            evidence=state.get("evidence", []),
            observations=state.get("observations", []),
            execution_plan=state.get("execution_plan", {}),
            budget=state.get("budget", {}),
            execution_trace=state.get("execution_trace", []),
        )

    def _ensure_active(self, state: AgentState) -> None:
        """检查时间、成本、调用次数及外部取消标记；失败即阻止下一副作用。"""
        budget = self._budget(state)
        self.budget_guard.ensure_active(budget)
        if self.cancellation_checker and self.cancellation_checker(
            state.get("tenant_id", ""),
            state.get("run_id", ""),
        ):
            raise RuntimeCancelled("Run cancellation was requested.")

    @staticmethod
    def _workflow_next_node(state: AgentState, action: AgentAction) -> str:
        """验证模型动作是当前发布节点允许的直接迁移，并返回目标节点。

        Graph DSL 不会生成 Python 代码；它只能让现有 Runtime 安全节点在预先发布的
        邻接关系中移动。缺失策略仅保留给未版本化的本地测试入口。
        """
        workflow = state.get("compiled_plan", {}).get("workflow_policy", {})
        if not workflow or workflow.get("local_development_only"):
            return ""
        cursor = str(state.get("workflow_cursor") or workflow.get("entrypoint") or "")
        roles = workflow.get("node_roles", {})
        adjacency = workflow.get("adjacency", {})
        role = roles.get(cursor)
        requested_role = {
            AgentAction.RETRIEVE: "retrieval",
            AgentAction.TOOL: "tool",
            AgentAction.ANSWER: "answer",
        }[action]
        if role == "answer" and action == AgentAction.ANSWER and cursor in workflow.get("terminals", []):
            return cursor
        if role != "decision":
            raise RuntimeLimitExceeded(
                "WORKFLOW_DECISION_FORBIDDEN",
                f"Published workflow node '{cursor}' cannot request a model action.",
            )
        facts = AgentGraph._workflow_facts(state, action=action)
        matches = [
            item["to"]
            for item in adjacency.get(cursor, [])
            if roles.get(item["to"]) == requested_role
            and evaluate_workflow_condition(item.get("condition"), facts)
        ]
        if len(matches) != 1:
            raise RuntimeLimitExceeded(
                "WORKFLOW_ACTION_FORBIDDEN",
                f"Published workflow does not allow action {action.value} from '{cursor}'.",
            )
        return matches[0]

    @staticmethod
    def _workflow_after_side_effect(state: AgentState, expected_role: str) -> str:
        """完成检索或工具后沿发布图移动；一对多或错类型迁移一律失败关闭。"""
        workflow = state.get("compiled_plan", {}).get("workflow_policy", {})
        if not workflow or workflow.get("local_development_only"):
            return state.get("workflow_cursor", "")
        cursor = str(state.get("workflow_cursor", ""))
        roles = workflow.get("node_roles", {})
        adjacency = workflow.get("adjacency", {})
        if roles.get(cursor) != expected_role:
            raise RuntimeLimitExceeded(
                "WORKFLOW_CURSOR_INVALID",
                f"Published workflow expected {expected_role} at '{cursor}'.",
            )
        facts = AgentGraph._workflow_facts(state)
        targets = [
            item["to"]
            for item in adjacency.get(cursor, [])
            if evaluate_workflow_condition(item.get("condition"), facts)
        ]
        if len(targets) != 1 or roles.get(targets[0]) not in {"decision", "answer", "clarify"}:
            raise RuntimeLimitExceeded(
                "WORKFLOW_TRANSITION_FORBIDDEN",
                f"Published workflow has no valid successor for '{cursor}'.",
            )
        return targets[0]

    @staticmethod
    def _workflow_facts(
        state: AgentState,
        *,
        action: AgentAction | None = None,
    ) -> dict[str, Any]:
        """只暴露发布 DSL 白名单中的运行事实，避免条件读取请求任意字段。"""
        decision = state.get("decision", {})
        selected_action = action.value if action is not None else decision.get("action")
        tool_observations = [
            item for item in state.get("observations", []) if item.get("type") == "tool"
        ]
        latest_tool = tool_observations[-1] if tool_observations else {}
        budget = AgentGraph._budget(state)
        intent = state.get("intent", {})
        return {
            "decision.action": selected_action,
            "intent.name": intent.get("name"),
            "intent.confidence": intent.get("confidence"),
            "evidence.count": len(state.get("evidence", [])),
            "tool.success": latest_tool.get("success"),
            "budget.remaining_cost_usd": budget.remaining_cost_usd,
            "budget.remaining_ms": budget.remaining_ms,
        }

    @staticmethod
    def _budget(state: AgentState) -> RuntimeBudget:
        """反序列化已有预算，或为本地兼容调用构造保守默认预算。

        默认值只服务开发入口；生产请求应由运行 API 提供明确 deadline 与限制，
        不能把此兼容路径当作无限额度。
        """
        existing = state.get("budget")
        if existing:
            return RuntimeBudget.model_validate(existing)
        deadline = state.get("deadline_at")
        deadline_at = (
            datetime.fromisoformat(deadline)
            if deadline
            else datetime.now(UTC) + timedelta(seconds=60)
        )
        return RuntimeBudget(
            deadline_at=deadline_at,
            max_steps=state.get("max_steps", 8),
            max_llm_calls=100,
            max_tool_calls=100,
            max_retrieval_rounds=100,
            max_cost_usd=100,
            max_attempts=state.get("attempt_budget_remaining", 100),
        )

    @staticmethod
    def _trace(
        state: AgentState,
        node: str,
        detail: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """以追加方式记录节点级审计事件，避免节点覆盖此前的执行轨迹。"""
        return [
            *state.get("execution_trace", []),
            {
                "node": node,
                "at": datetime.now(UTC).isoformat(),
                "detail": detail,
            },
        ]

    @staticmethod
    def _execution_headers(state: AgentState) -> dict[str, str]:
        """向内部服务转发租户隔离、追踪和剩余预算，不透传原始用户 Header。"""
        budget = AgentGraph._budget(state)
        return {
            "X-Tenant-Id": state.get("tenant_id", ""),
            "X-User-Id": state.get("user_id", ""),
            "X-Request-Id": state.get("request_id", ""),
            "X-Trace-Id": state.get("trace_id", ""),
            "X-Run-Id": state.get("run_id", ""),
            "X-Session-Id": state.get("session_id", ""),
            "X-Agent-Id": state.get("agent_id", ""),
            "X-Agent-Version": state.get("agent_version", ""),
            "X-Snapshot-Id": state.get("snapshot_id", ""),
            "X-Deadline-At": budget.deadline_at.isoformat(),
            "X-Attempt-Budget-Remaining": str(budget.remaining_attempts),
        }

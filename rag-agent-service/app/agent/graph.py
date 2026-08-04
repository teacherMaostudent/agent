from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from inspect import signature
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from opentelemetry import trace

from app.agent.decision_engine import DecisionEngine
from app.agent.models import AgentAction, AgentDecision, AgentRunResult, AgentState
from app.contracts.context import ContextAssembleRequest
from app.ingestion.chunker import TextChunker
from app.retrieval.controlled_scan import bound_untrusted
from app.runtime.budget import BudgetGuard
from app.runtime.models import (
    ApprovalResume,
    RouteType,
    RuntimeBudget,
    RuntimeCancelled,
    RuntimeLimitExceeded,
)
from app.runtime.planner import HeuristicSemanticAnalyzer, RuntimePlanner
from app.runtime.snapshot_compiler import validate_final_output
from app.tools.registry import ToolContext, ToolRegistry


class _LegacyRetrievalContext:
    """Compatibility adapter for tests and the old all-in-one application."""

    def __init__(self, retriever, repository) -> None:
        self.retriever = retriever
        self.repository = repository
        self.chunker = TextChunker()

    def assemble(
        self,
        request: ContextAssembleRequest,
        *,
        execution_headers: dict[str, str] | None = None,
    ):
        del execution_headers
        from app.contracts.context import ContextPackage

        if not request.include_rag:
            return ContextPackage(
                session_id=request.session_id,
                user_context={
                    "tenant_id": request.tenant_id,
                    "user_id": request.user_id,
                },
                token_budget=12000,
                estimated_tokens=0,
            )
        # Legacy local mode retrieves only documents supplied by the caller;
        # production retrieval is delegated to the RAG service.
        chunks = []
        if request.document_id and self.repository.get_document(request.document_id):
            chunks.extend(self.repository.document_chunks(request.document_id))
        if request.content:
            chunks.extend(
                self.chunker.chunk(
                    source_id=f"inline:{request.user_id}:{request.session_id}",
                    source_type="enterprise_document",
                    text=request.content,
                    metadata={"temporary": True},
                )
            )
        evidence = self.retriever.search(request.query, chunks, request.top_k)
        return ContextPackage(
            session_id=request.session_id,
            knowledge_evidence=evidence,
            user_context={"tenant_id": request.tenant_id, "user_id": request.user_id},
            token_budget=12000,
            estimated_tokens=sum(max(1, len(item.text) // 4) for item in evidence),
        )


class AgentGraph:
    """Bounded decide -> retrieve/tool -> observe loop with a deterministic safety exit.

    LangGraph provides the durable graph mechanics; this class owns platform
    semantics: published-plan enforcement, Context assembly, budget checks,
    approval interrupts and sanitised observations passed back to the model.
    """

    def __init__(
        self,
        decision_engine: DecisionEngine,
        retriever=None,
        repository=None,
        tool_registry: ToolRegistry | None = None,
        *,
        context_client=None,
        planner: RuntimePlanner | None = None,
        budget_guard: BudgetGuard | None = None,
        checkpointer=None,
        cancellation_checker: Callable[[str, str], bool] | None = None,
    ) -> None:
        self.decision_engine = decision_engine
        if context_client is None:
            if retriever is None or repository is None:
                raise ValueError("context_client or retriever/repository is required")
            context_client = _LegacyRetrievalContext(retriever, repository)
        self.context_client = context_client
        self._context_accepts_execution_headers = (
            "execution_headers" in signature(context_client.assemble).parameters
        )
        self.tool_registry = tool_registry or ToolRegistry()
        self.planner = planner or RuntimePlanner(HeuristicSemanticAnalyzer())
        self.budget_guard = budget_guard or BudgetGuard(0.0, 0.0)
        self.cancellation_checker = cancellation_checker
        self.graph = self._build().compile(checkpointer=checkpointer or InMemorySaver())

    def _build(self):
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
        graph.add_edge("planned_retrieve", "decide")
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
        graph.add_edge("retrieve", "decide")
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
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": max(30, max_steps * 5 + 15),
        }

    def _analyze(self, state: AgentState) -> dict:
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
        self._ensure_active(state)
        plan = self.planner.build_plan(state)
        return {
            "execution_plan": plan.model_dump(mode="json"),
            "execution_trace": self._trace(
                state,
                "build_plan",
                {"route": plan.route.route.value, "complexity": plan.complexity.score},
            ),
        }

    @staticmethod
    def _plan_route(state: AgentState) -> str:
        route = state["execution_plan"]["route"]["route"]
        if route == RouteType.CLARIFY:
            return "clarify"
        if route == RouteType.RAG:
            return "rag"
        return "agent"

    def _decide(self, state: AgentState) -> dict:
        self._ensure_active(state)
        budget = self.budget_guard.count_step(self._budget(state))
        if getattr(self.decision_engine, "uses_llm", False):
            budget = self.budget_guard.reserve_llm(budget)
        working = {**state, "budget": budget.model_dump(mode="json")}
        with trace.get_tracer(__name__).start_as_current_span("agent.decide"):
            decision = self.decision_engine.decide(working, self.tool_registry)
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
            "step_count": state.get("step_count", 0) + 1,
            "budget": budget.model_dump(mode="json"),
            "execution_trace": self._trace(
                state,
                "decide",
                {"action": decision.action.value},
            ),
        }

    def _route(self, state: AgentState) -> str:
        if state.get("step_count", 0) >= state["max_steps"]:
            return "limit"
        action = AgentDecision.model_validate(state["decision"]).action
        return {
            AgentAction.RETRIEVE: "retrieve",
            AgentAction.TOOL: "tool",
            AgentAction.ANSWER: "answer",
        }[action]

    def _retrieve(self, state: AgentState) -> dict:
        decision = AgentDecision.model_validate(state["decision"])
        return self._do_retrieve(state, decision.query, "retrieve")

    def _planned_retrieve(self, state: AgentState) -> dict:
        return self._do_retrieve(state, state["task"], "planned_retrieve")

    def _retrieval_guard(self, state: AgentState) -> dict:
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
                include_rag=not no_rag,
                rag_required=False if no_rag else self._rag_required(state),
            )
            if self._context_accepts_execution_headers:
                package = self.context_client.assemble(
                    context_request,
                    execution_headers=self._execution_headers(state),
                )
            else:
                package = self.context_client.assemble(context_request)
            span.set_attribute("rag.retrieved_documents", len(package.knowledge_evidence))
            span.set_attribute("context.estimated_tokens", package.estimated_tokens)
        evidence = [item.model_dump(mode="json") for item in package.knowledge_evidence]
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
            "context_degraded": package.degraded,
            "rag_status": package.rag_status,
            "retrieval_profile": policy.get("profile", "STANDARD"),
        }
        return {
            "evidence": [*state.get("evidence", []), *evidence],
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
            "observations": [*state.get("observations", []), observation],
            "execution_trace": self._trace(
                state,
                node_name,
                {"result_count": len(evidence)},
            ),
        }

    @staticmethod
    def _rag_required(state: AgentState) -> bool:
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
        self._ensure_active(state)
        budget = self.budget_guard.reserve_tool(self._budget(state))
        return {"budget": budget.model_dump(mode="json")}

    def _tool(self, state: AgentState) -> dict:
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
            "execution_trace": self._trace(
                state,
                "tool",
                {"tool": decision.tool_name, "success": observation["success"]},
            ),
        }

    @staticmethod
    def _after_tool(state: AgentState) -> str:
        return "finish" if state.get("termination_reason") == "TOOL_REJECTED" else "continue"

    @staticmethod
    def _tool_context(
        state: AgentState,
        approval_id: str,
        tool_version: str,
    ) -> ToolContext:
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
        return {
            "final_answer": (
                "Please clarify the request so that its intent can be determined safely."
            ),
            "termination_reason": "NEEDS_CLARIFICATION",
            "execution_trace": self._trace(state, "clarify", {}),
        }

    def _finalize(self, state: AgentState) -> dict:
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
        budget = self._budget(state)
        self.budget_guard.ensure_active(budget)
        if self.cancellation_checker and self.cancellation_checker(
            state.get("tenant_id", ""),
            state.get("run_id", ""),
        ):
            raise RuntimeCancelled("Run cancellation was requested.")

    @staticmethod
    def _budget(state: AgentState) -> RuntimeBudget:
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

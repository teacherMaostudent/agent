"""LangGraph implementation of the bounded Runtime action loop.

LangGraph persists graph control flow; this module adds platform invariants
around published snapshots, approvals, budgets and untrusted observation data.
"""

from __future__ import annotations

import hashlib
import json
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
from platform_sdk.contracts.execution import ExecutionContext
from platform_sdk.contracts.orchestration import TaskPlan, TaskPlanStep
from platform_sdk.contracts.rag import RagSearchRequest
from platform_sdk.contracts.skills import (
    CapabilityProviderDescriptor,
    CapabilityProviderKind,
    CapabilityResolutionRequest,
    CapabilityRoutingPolicy,
)
from platform_sdk.contracts.subagents import CapabilityRequirement, ConflictStrategy
from platform_sdk.contracts.workflow import evaluate_workflow_condition
from platform_sdk.security import bound_untrusted
from platform_sdk.tools.registry import ToolContext

from agent_runtime_service.agent.decision_engine import DecisionEngine
from agent_runtime_service.agent.models import (
    AgentAction,
    AgentDecision,
    AgentRunResult,
    AgentState,
)
from agent_runtime_service.runtime.agent_manager import AgentManager
from agent_runtime_service.runtime.budget import BudgetGuard
from agent_runtime_service.runtime.capability_evaluator import ToolCapabilityEvaluator
from agent_runtime_service.runtime.capability_resolver import BusinessCapabilityResolver
from agent_runtime_service.runtime.collaboration import CollaborationError, ResultResolver
from agent_runtime_service.runtime.event_bus import RuntimeHookPhase, RuntimeInterceptionPipeline
from agent_runtime_service.runtime.mailbox import RunMailbox, RunMailboxInputType
from agent_runtime_service.runtime.models import (
    ApprovalResume,
    ProposedExecutionPlan,
    RouteType,
    RuntimeBudget,
    RuntimeCancelled,
    RuntimeLimitExceeded,
    UserInputResume,
)
from agent_runtime_service.runtime.plan_admission import (
    PlanAdmissionRejected,
    PlanAdmissionService,
)
from agent_runtime_service.runtime.planner import HeuristicSemanticAnalyzer, RuntimePlanner
from agent_runtime_service.runtime.prompt_security import PromptSecurityGuard
from agent_runtime_service.runtime.reference_monitor import RuntimeReferenceMonitor
from agent_runtime_service.runtime.runtime_context import RuntimeContext
from agent_runtime_service.runtime.session_events import (
    ModelVisibleMessage,
    RuntimeEventType,
    deterministic_tool_execution_id,
    model_visible_message,
)
from agent_runtime_service.runtime.snapshot_compiler import validate_final_output
from agent_runtime_service.runtime.stop_policy import (
    BudgetStopPolicy,
    CancellationStopPolicy,
    CompositeStopPolicy,
    StopPolicy,
)
from agent_runtime_service.runtime.subagents import SubAgentPolicyError
from agent_runtime_service.runtime.tool_evidence import ToolEvidencePipeline
from agent_runtime_service.runtime.tool_execution import (
    SideEffectBarrier,
    SideEffectBarrierOutcome,
    ToolExecutionEngine,
    ToolExecutionPolicy,
)
from agent_runtime_service.runtime.workflow_runtime import WorkflowSuspended


class AgentGraph:
    """Bounded decide -> retrieve/tool -> observe loop with a deterministic safety exit.

    LangGraph provides the durable graph mechanics; this class owns platform
    semantics: published-plan enforcement, Context assembly, budget checks,
    approval interrupts and sanitised observations passed back to the model.
    """

    def __init__(
        self,
        decision_engine: DecisionEngine,
        runtime_context: RuntimeContext,
        *,
        planner: RuntimePlanner | None = None,
        budget_guard: BudgetGuard | None = None,
        checkpointer=None,
        cancellation_checker: Callable[[str, str], bool] | None = None,
        agent_manager: AgentManager | None = None,
        session_event_recorder: Callable[
            [AgentState, RuntimeEventType, dict[str, Any], ModelVisibleMessage | None], None
        ]
        | None = None,
        interception_pipeline: RuntimeInterceptionPipeline | None = None,
        stop_policy: StopPolicy | None = None,
        tool_execution_engine: ToolExecutionEngine | None = None,
        mailbox: RunMailbox | None = None,
        side_effect_barrier: SideEffectBarrier | None = None,
        capability_handler_factory: Callable[[AgentState], dict[CapabilityProviderKind, Callable]]
        | None = None,
        plan_admission: PlanAdmissionService | None = None,
    ) -> None:
        """组装受控执行图。

        ``runtime_context`` 是执行器唯一可见的外围能力视图：Context 是会话与 ACL
        的唯一所有者，RAG 仅能经稳定契约读取证据；二者均不得让 Runtime 直接接触
        其内部存储。可选取消检查器在
        每个有副作用节点前执行，使 API 取消能够在长任务中尽快生效。
        """
        self.decision_engine = decision_engine
        self.runtime_context = runtime_context
        self.context_client = runtime_context.context
        # Evidence is read only through the published RAG contract. Context
        # owns conversation memory and its ACL boundary; Runtime owns neither.
        self.rag_client = runtime_context.retrieval
        self._context_accepts_execution_headers = (
            "execution_headers" in signature(self.context_client.assemble).parameters
        )
        self.tool_registry = runtime_context.tools
        self.planner = planner or RuntimePlanner(HeuristicSemanticAnalyzer())
        self.budget_guard = budget_guard or BudgetGuard(0.0, 0.0)
        # StopPolicy 是模型循环外的硬边界。默认策略复用既有预算守卫与取消查询，
        # 外部执行器可在启动期注入更严格策略，但不能在请求期动态替换。
        self.stop_policy = stop_policy or CompositeStopPolicy(
            (BudgetStopPolicy(self.budget_guard), CancellationStopPolicy(cancellation_checker))
        )
        # Agent Manager 是子 Agent 的唯一委派门面。Graph 只提交已决策的目标和
        # 任务，不持有预算策略或内部调用器，避免状态机演变为万能编排服务。
        self.agent_manager = agent_manager
        # Recorder is an append-only audit boundary owned by Runtime Store. It observes completed facts
        # only and cannot replace a Tool Gateway, alter a LangGraph edge or make policy decisions.
        self.session_event_recorder = session_event_recorder
        self.interception_pipeline = interception_pipeline or RuntimeInterceptionPipeline()
        self.tool_execution_engine = tool_execution_engine or ToolExecutionEngine()
        self.prompt_security = PromptSecurityGuard()
        # 工具执行成功不等于其输出可被模型当作事实；该流水线只写入本次执行状态。
        self.tool_evidence_pipeline = ToolEvidencePipeline(self.prompt_security)
        self.mailbox = mailbox
        self.side_effect_barrier = side_effect_barrier or SideEffectBarrier(
            cancellation_checker=cancellation_checker,
            inbox=mailbox,
        )
        self.capability_evaluator = ToolCapabilityEvaluator()
        self.reference_monitor = RuntimeReferenceMonitor(self.side_effect_barrier)
        self.capability_handler_factory = capability_handler_factory
        # Planner 只能提出计划；准入服务在执行图进入任何业务节点前生成独立凭证。
        self.plan_admission = plan_admission or PlanAdmissionService()
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
        graph.add_node("admit_plan", self._admit_plan)
        graph.add_node("planned_retrieval_guard", self._retrieval_guard)
        graph.add_node("planned_retrieve", self._planned_retrieve)
        graph.add_node("poll_mailbox", self._poll_mailbox)
        graph.add_node("decide", self._decide)
        graph.add_node("retrieval_guard", self._retrieval_guard)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("tool_guard", self._tool_guard)
        graph.add_node("tool", self._tool)
        graph.add_node("tool_evidence", self._tool_evidence)
        graph.add_node("resolve_capability", self._resolve_capability)
        graph.add_node("execute_capability", self._execute_capability)
        graph.add_node("clarify", self._clarify)
        graph.add_node("finalize", self._finalize)
        graph.add_node("safety", self._safety)
        graph.add_edge(START, "load_memory")
        graph.add_edge("load_memory", "analyze")
        graph.add_edge("analyze", "build_plan")
        graph.add_edge("build_plan", "admit_plan")
        graph.add_conditional_edges(
            "admit_plan",
            self._plan_route,
            {
                "clarify": "clarify",
                "rag": "planned_retrieval_guard",
                "agent": "poll_mailbox",
            },
        )
        graph.add_edge("planned_retrieval_guard", "planned_retrieve")
        graph.add_conditional_edges(
            "planned_retrieve",
            self._after_retrieval,
            {"decide": "poll_mailbox", "clarify": "clarify"},
        )
        graph.add_conditional_edges(
            "decide",
            self._route,
            {
                "retrieve": "retrieval_guard",
                "tool": "tool_guard",
                "capability": "resolve_capability",
                "answer": "finalize",
                "limit": "finalize",
            },
        )
        graph.add_edge("retrieval_guard", "retrieve")
        graph.add_conditional_edges(
            "resolve_capability",
            self._after_capability_resolution,
            {
                "retrieve": "retrieval_guard",
                "tool": "tool_guard",
                "execute": "execute_capability",
            },
        )
        graph.add_edge("execute_capability", "poll_mailbox")
        graph.add_conditional_edges(
            "retrieve",
            self._after_retrieval,
            {"decide": "poll_mailbox", "clarify": "clarify"},
        )
        graph.add_conditional_edges(
            "tool_guard",
            self._after_tool_guard,
            {"tool": "tool", "defer": "poll_mailbox"},
        )
        graph.add_edge("tool", "tool_evidence")
        graph.add_conditional_edges(
            "tool_evidence",
            self._after_tool,
            {"continue": "poll_mailbox", "finish": "safety"},
        )
        graph.add_conditional_edges(
            "poll_mailbox",
            self._after_mailbox,
            {"replan": "load_memory", "decide": "decide"},
        )
        # 澄清节点以 LangGraph interrupt 挂起；收到邮箱输入后回到 Context/Planner，而非结束旧 Run。
        graph.add_edge("clarify", "load_memory")
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
            return self._limited_result(self._checkpoint_state(config, initial), exc)
        except RuntimeCancelled:
            return self._cancelled_result(self._checkpoint_state(config, initial))

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
            return self._limited_result(self._checkpoint_state(self._config(thread_id, max_steps)), exc)
        except RuntimeCancelled:
            return self._cancelled_result(self._checkpoint_state(self._config(thread_id, max_steps)))

    def _checkpoint_state(self, config: dict[str, Any], fallback: dict | None = None) -> dict:
        """在预期终止后读取最近检查点，使结果保留真实预算、观察和执行轨迹。"""
        try:
            snapshot = self.graph.get_state(config)
            values = getattr(snapshot, "values", None)
            if isinstance(values, dict) and values:
                return values
        except Exception:
            # 结果整理不能掩盖原始限额/取消原因；检查点后端不可用时退回已有状态。
            pass
        return fallback or {}

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
        root_reservation_id = ""
        if self.planner.analyzer.uses_llm:
            budget = self.budget_guard.reserve_llm(budget)
            root_reservation_id = self._reserve_root_budget(
                state, "analyze", cost_usd=self.budget_guard.llm_call_reservation_usd, steps=0
            )
        working = {**state, "budget": budget.model_dump(mode="json")}
        self.interception_pipeline.apply(
            RuntimeHookPhase.PRE_PROMPT,
            self._hook_payload(state, {"step": state.get("step_count", 0)}),
        )
        try:
            analysis = self.planner.analyze(working)
        except Exception:
            self._settle_root_budget(root_reservation_id, actual_cost_usd=0.0, actual_steps=0)
            raise
        if self.planner.analyzer.uses_llm:
            cost_reader = getattr(self.planner.analyzer, "last_cost_usd", None)
            actual_cost = cost_reader() if cost_reader else None
            budget = self.budget_guard.reconcile_cost(
                budget,
                reserved_usd=self.budget_guard.llm_call_reservation_usd,
                actual_usd=actual_cost,
            )
            self._settle_root_budget(
                root_reservation_id,
                actual_cost_usd=(
                    actual_cost
                    if actual_cost is not None
                    else self.budget_guard.llm_call_reservation_usd
                ),
                actual_steps=0,
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
        next_task = state["task"]
        if state.get("mailbox_replan"):
            # 新输入正文只存在 Context；Runtime 仅以租约引用它，并将最新用户消息设为下一轮任务。
            latest = next(
                (item.content for item in reversed(package.recent_messages) if item.role == "user"),
                "",
            )
            if latest.strip():
                next_task = latest
        history = [
            item.model_dump(mode="json")
            for item in package.recent_messages
            if not (item.role == "user" and item.content == next_task)
        ]
        self._record_session_event(
            state,
            RuntimeEventType.CONTEXT_INJECTED,
            {
                "context_source": "context-service",
                "history_count": len(history),
                "selection_policy": "context-service",
                "budget_report": (
                    package.budget_report.model_dump(mode="json") if package.budget_report else None
                ),
            },
        )
        updates = {
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
        if state.get("mailbox_replan"):
            if self.mailbox is None or not self.mailbox.acknowledge_mailbox_input(
                str(state.get("mailbox_message_id", "")), str(state.get("mailbox_lease_token", ""))
            ):
                raise RuntimeLimitExceeded(
                    "MAILBOX_LEASE_LOST", "Mailbox input lease could not be confirmed."
                )
            updates.update(
                {
                    "task": next_task,
                    "mailbox_replan": False,
                    "mailbox_message_id": "",
                    "mailbox_lease_token": "",
                }
            )
        return updates

    def _poll_mailbox(self, state: AgentState) -> dict:
        """在模型/工具之间的安全点领取一条外部输入，并强制回到 Context 重装与规划。"""
        self._ensure_active(state)
        if self.mailbox is None:
            return {"mailbox_replan": False}
        item = self.mailbox.claim_mailbox_input(
            str(state.get("tenant_id", "")), str(state.get("run_id", ""))
        )
        if item is None:
            return {"mailbox_replan": False}
        # 每次重新规划消耗一个 Agent step，防止持续 Steering 把单次 Run 变成无限会话循环。
        budget = self.budget_guard.count_step(self._budget(state))
        # 所有控制输入先记录统一事实，再追加其业务语义事件。这样审批、Signal 唤醒和
        # 普通 Steering 能用相同的 Run/Turn/Attempt 维度审计，而不会复制消息正文。
        self._record_session_event(
            state,
            RuntimeEventType.RUN_INPUT_RECEIVED,
            {"mailbox_message_id": item.message_id, "input_type": item.input_type.value},
        )
        event_type = (
            RuntimeEventType.STEERING_RECEIVED
            if item.input_type == RunMailboxInputType.STEERING
            else RuntimeEventType.FOLLOW_UP_RECEIVED
        )
        self._record_session_event(
            state,
            event_type,
            {"mailbox_message_id": item.message_id, "input_type": item.input_type.value},
        )
        return {
            "mailbox_replan": True,
            "mailbox_message_id": item.message_id,
            "mailbox_lease_token": item.lease_token,
            "budget": budget.model_dump(mode="json"),
            "execution_trace": self._trace(state, "mailbox", {"input_type": item.input_type.value}),
        }

    @staticmethod
    def _after_mailbox(state: AgentState) -> str:
        """领取输入时必须重新读取 Context 与 Planner；没有输入才继续当前决定循环。"""
        return "replan" if state.get("mailbox_replan") else "decide"

    def _build_plan(self, state: AgentState) -> dict:
        """提出路由、SLA 与检索计划；此阶段不授予任何执行或副作用权限。"""
        self._ensure_active(state)
        plan = self.planner.build_plan(state)
        workflow = state.get("compiled_plan", {}).get("workflow_policy", {})
        capability_ids = plan.capability_policy.required
        task_plan = TaskPlan(
            task_plan_id=f"taskplan_{state.get('run_id', state.get('request_id', ''))}",
            goal=state["task"],
            steps=(
                [
                    TaskPlanStep(
                        step_id=f"capability-{index + 1}",
                        objective=f"完成发布计划要求的能力 {capability_id}",
                        capability_id=capability_id,
                        execution_strategy="DETERMINISTIC",
                    )
                    for index, capability_id in enumerate(capability_ids)
                ]
                or [
                    TaskPlanStep(
                        step_id="resolve-request",
                        objective=state["task"],
                        execution_strategy="REACT",
                    )
                ]
            ),
            revision=int(state.get("task_plan", {}).get("revision", 0)) + 1,
        )
        return {
            "proposed_execution_plan": plan.model_dump(mode="json"),
            # 保留旧字段供准入节点和迁移期 Trace 读取；下一节点会用准入计划覆盖它。
            "execution_plan": plan.model_dump(mode="json"),
            "task_plan": task_plan.model_dump(mode="json"),
            # Cursor is a published Graph node, not a model-selected string.
            # Every later action advances it through the compiled adjacency map.
            "workflow_cursor": workflow.get("entrypoint", ""),
            "execution_trace": self._trace(
                state,
                "build_plan",
                {
                    "plan_stage": "PROPOSED",
                    "plan_id": plan.plan_id,
                    "route": plan.route.route.value,
                    "complexity": plan.complexity.score,
                },
            ),
        }

    def _admit_plan(self, state: AgentState) -> dict:
        """在进入执行引擎前执行计划级硬检查，并保存独立准入凭证。"""
        self._ensure_active(state)
        proposed = ProposedExecutionPlan.model_validate(
            state.get("proposed_execution_plan") or state.get("execution_plan")
        )
        try:
            admitted = self.plan_admission.admit(
                proposed,
                compiled_plan=state.get("compiled_plan", {}),
                caller_permissions=state.get("permissions", []),
            )
        except PlanAdmissionRejected as exc:
            self._record_session_event(
                state,
                RuntimeEventType.PLAN_REJECTED,
                exc.decision.model_dump(mode="json"),
            )
            raise
        admission = {
            "admission_id": admitted.admission_id,
            "plan_id": admitted.plan_id,
            "policy_version": admitted.admission_policy_version,
            "checks": [item.model_dump(mode="json") for item in admitted.admission_checks],
            "allowed_tool_scope": admitted.allowed_tool_scope,
        }
        self._record_session_event(
            state,
            RuntimeEventType.PLAN_ADMITTED,
            admission,
        )
        return {
            "execution_plan": admitted.model_dump(mode="json"),
            "plan_admission": admission,
            "execution_trace": self._trace(state, "plan_admission", admission),
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
            raise RuntimeLimitExceeded(
                "WORKFLOW_ENTRY_INVALID", "Published workflow entry is invalid."
            )
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
        step_id = self._step_id(state, pending=True)
        epoch_id = f"epoch_{step_id}"
        budget = self.budget_guard.count_step(self._budget(state))
        root_reservation_id = ""
        if getattr(self.decision_engine, "uses_llm", False):
            budget = self.budget_guard.reserve_llm(budget)
            root_reservation_id = self._reserve_root_budget(
                state,
                f"decide:{step_id}",
                cost_usd=self.budget_guard.llm_call_reservation_usd,
                steps=1,
            )
        working = {**state, "budget": budget.model_dump(mode="json")}
        _, injection_findings = self.prompt_security.prepare_model_input(working)
        self._record_session_event(
            state,
            RuntimeEventType.STEP_STARTED,
            {"step": state.get("step_count", 0) + 1, "step_id": step_id},
        )
        self._record_session_event(
            state,
            RuntimeEventType.REQUEST_EPOCH_PINNED,
            {
                "step_id": step_id,
                "epoch_id": epoch_id,
                "model_route": str(state.get("compiled_plan", {}).get("logical_model", "")),
                "model_policy_version": str(state.get("model_policy_version", "")),
                "model_revision": str(state.get("compiled_plan", {}).get("logical_model", "")),
                "prompt_version": str(state.get("compiled_plan", {}).get("contract_hash", "")),
                "rendered_prompt_hash": self._epoch_hash(
                    {
                        "template": state.get("compiled_plan", {}).get("prompt_template", ""),
                        "task": state.get("task", ""),
                        "history": state.get("conversation_history", []),
                        "evidence": state.get("evidence", []),
                    }
                ),
                "tool_catalog_version": str(
                    state.get("compiled_plan", {}).get("tool_catalog_version", "")
                ),
                "visible_tool_schema_hash": self._epoch_hash(
                    state.get("compiled_plan", {}).get("tools", [])
                ),
                "knowledge_bindings": state.get("compiled_plan", {}).get("knowledge", []),
                "index_contracts": {
                    str(item.get("knowledge_base", "")): {
                        "index_version": str(item.get("index_version", "")),
                        "embedding_contract_id": str(item.get("embedding_contract_id", "")),
                    }
                    for item in state.get("compiled_plan", {}).get("knowledge", [])
                },
                "budget_policy": state.get("budget", {}),
                "retrieval_policy": state.get("execution_plan", {}).get("retrieval_policy", {}),
                "context_sources": state.get("source_plan", {}).get("context_sources", []),
                "output_schema": state.get("compiled_plan", {}).get("prompt_output_schema", {}),
            },
        )
        self._record_session_event(
            state,
            RuntimeEventType.PROMPT_ASSEMBLED,
            {
                "prompt_version": str(state.get("compiled_plan", {}).get("contract_hash", "")),
                "history_count": len(state.get("conversation_history", [])),
                "evidence_count": len(state.get("evidence", [])),
                "tool_count": len(state.get("compiled_plan", {}).get("tools", [])),
                "prompt_injection_findings": [
                    {"code": item.code, "severity": item.severity, "source_id": item.source_id}
                    for item in injection_findings
                ],
                "step_id": step_id,
                "epoch_id": epoch_id,
            },
            model_visible_message("user", state.get("task", ""), source="runtime.prompt.task"),
        )
        self._record_session_event(
            state,
            RuntimeEventType.MODEL_REQUESTED,
            {
                "logical_model": str(state.get("compiled_plan", {}).get("logical_model", "")),
                "step_id": step_id,
                "epoch_id": epoch_id,
            },
        )
        self.interception_pipeline.apply(
            RuntimeHookPhase.PRE_MODEL_REQUEST,
            self._hook_payload(
                state,
                {"logical_model": str(state.get("compiled_plan", {}).get("logical_model", ""))},
            ),
        )
        try:
            with trace.get_tracer(__name__).start_as_current_span("agent.decide"):
                decision = self.decision_engine.decide(working, self.tool_registry)
        except Exception:
            self._settle_root_budget(root_reservation_id, actual_cost_usd=0.0, actual_steps=0)
            raise
        self.interception_pipeline.apply(
            RuntimeHookPhase.POST_MODEL_RESPONSE,
            self._hook_payload(state, {"action": decision.action.value}),
        )
        self._record_session_event(
            state,
            RuntimeEventType.MODEL_RESPONDED,
            {
                "action": decision.action.value,
                "step": state.get("step_count", 0) + 1,
                "step_id": step_id,
                "epoch_id": epoch_id,
            },
            model_visible_message(
                "assistant", decision.model_dump_json(), source="runtime.model.decision"
            ),
        )
        next_node = (
            str(state.get("workflow_cursor", ""))
            if decision.action == AgentAction.CAPABILITY
            else self._workflow_next_node(state, decision.action)
        )
        if getattr(self.decision_engine, "uses_llm", False):
            cost_reader = getattr(self.decision_engine, "last_cost_usd", None)
            actual_cost = cost_reader() if cost_reader else None
            budget = self.budget_guard.reconcile_cost(
                budget,
                reserved_usd=self.budget_guard.llm_call_reservation_usd,
                actual_usd=actual_cost,
            )
            self._settle_root_budget(
                root_reservation_id,
                actual_cost_usd=(
                    actual_cost
                    if actual_cost is not None
                    else self.budget_guard.llm_call_reservation_usd
                ),
                actual_steps=1,
            )
        self.interception_pipeline.apply(
            RuntimeHookPhase.POST_STEP,
            self._hook_payload(
                state,
                {"step": state.get("step_count", 0) + 1, "action": decision.action.value},
            ),
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
            AgentAction.SUBAGENT: "tool",
            AgentAction.CAPABILITY: "capability",
            AgentAction.ANSWER: "answer",
        }[action]

    def _resolve_capability(self, state: AgentState) -> dict:
        """只从发布快照选择 Provider，模型无法指定实现或回退顺序。"""
        self._ensure_active(state)
        decision = AgentDecision.model_validate(state["decision"])
        compiled = state.get("compiled_plan", {})
        providers = [
            CapabilityProviderDescriptor.model_validate(item)
            for item in compiled.get("capability_providers", [])
        ]
        policies = {
            item.capability_id: item
            for item in (
                CapabilityRoutingPolicy.model_validate(value)
                for value in compiled.get("capability_routing", [])
            )
        }
        resolution_request = CapabilityResolutionRequest(
            capability_id=decision.capability_id,
            caller_permissions=frozenset(state.get("permissions", [])),
            max_cost_usd=self._budget(state).remaining_cost_usd,
            max_latency_ms=self._budget(state).remaining_ms,
            require_independent_authority=decision.require_independent_authority,
        )
        resolver = BusinessCapabilityResolver(providers)
        policy = policies.get(resolution_request.capability_id)
        provider = (
            resolver.resolve_with_policy(resolution_request, policy)
            if policy is not None
            else resolver.resolve(resolution_request)
        )
        update: dict[str, Any] = {
            "resolved_capability_provider": provider.model_dump(mode="json"),
            "workflow_cursor": self._workflow_next_capability_node(state, provider.kind),
            "execution_trace": self._trace(
                state,
                "resolve_capability",
                {
                    "capability_id": resolution_request.capability_id,
                    "provider_id": provider.provider_id,
                    "provider_kind": provider.kind.value,
                },
            ),
        }
        if provider.kind == CapabilityProviderKind.TOOL:
            update["decision"] = decision.model_copy(
                update={
                    "action": AgentAction.TOOL,
                    "tool_name": provider.provider_id,
                    "tool_arguments": decision.capability_input,
                }
            ).model_dump(mode="json")
        elif provider.kind == CapabilityProviderKind.RAG:
            update["decision"] = decision.model_copy(
                update={
                    "action": AgentAction.RETRIEVE,
                    "query": str(
                        decision.capability_input.get("query")
                        or decision.capability_input.get("task")
                        or state["task"]
                    ),
                }
            ).model_dump(mode="json")
        elif provider.kind == CapabilityProviderKind.AGENT:
            update["decision"] = decision.model_copy(
                update={
                    "action": AgentAction.SUBAGENT,
                    "subagent_id": provider.provider_id,
                    "subagent_capability": resolution_request.capability_id,
                    "subagent_task": str(decision.capability_input.get("task") or state["task"]),
                }
            ).model_dump(mode="json")
        return update

    @staticmethod
    def _after_capability_resolution(state: AgentState) -> str:
        """将 Tool/RAG/Agent 送回原安全节点，其他 Provider 使用受限处理器。"""
        kind = CapabilityProviderKind(state["resolved_capability_provider"]["kind"])
        if kind in {CapabilityProviderKind.TOOL, CapabilityProviderKind.AGENT}:
            return "tool"
        if kind == CapabilityProviderKind.RAG:
            return "retrieve"
        return "execute"

    def _execute_capability(self, state: AgentState) -> dict:
        """执行 Skill/Human/Workflow，结果仅以不可信观察返回 Agent。"""
        if self.capability_handler_factory is None:
            raise RuntimeLimitExceeded(
                "CAPABILITY_HANDLER_UNAVAILABLE", "Capability handlers are not deployed."
            )
        decision = AgentDecision.model_validate(state["decision"])
        provider = CapabilityProviderDescriptor.model_validate(
            state["resolved_capability_provider"]
        )
        handler = self.capability_handler_factory(state).get(provider.kind)
        if handler is None:
            raise RuntimeLimitExceeded(
                "CAPABILITY_PROVIDER_UNAVAILABLE",
                f"Provider kind is not deployed: {provider.kind.value}",
            )
        context = self._capability_execution_context(state)
        try:
            output = handler(provider, decision.capability_input, context)
        except WorkflowSuspended as exc:
            signal = interrupt(
                {
                    "type": "capability_signal",
                    "provider_id": provider.provider_id,
                    "reason": str(exc),
                    **exc.payload,
                }
            )
            output = handler(
                provider,
                {**decision.capability_input, "signal": signal},
                context,
            )
        observation = {
            "type": "capability",
            "capability_id": decision.capability_id,
            "provider_id": provider.provider_id,
            "provider_kind": provider.kind.value,
            "output": bound_untrusted(output, 8_000),
        }
        return {
            "observations": [*state.get("observations", []), observation],
            "resolved_capability_provider": {},
            "workflow_cursor": self._workflow_after_side_effect(state, "tool"),
            "execution_trace": self._trace(
                state,
                "execute_capability",
                {
                    "capability_id": decision.capability_id,
                    "provider_id": provider.provider_id,
                },
            ),
        }

    @staticmethod
    def _capability_execution_context(state: AgentState) -> ExecutionContext:
        """从 Run 状态重建统一上下文，Skill 不需要 AgentSession 内部对象。"""
        return ExecutionContext(
            request_id=str(state["request_id"]),
            trace_id=str(state.get("trace_id", state["request_id"])),
            run_id=str(state["run_id"]),
            parent_run_id=str(state.get("metadata", {}).get("_parent_run_id", "")),
            session_id=str(state.get("session_id", "")),
            parent_session_id=str(state.get("metadata", {}).get("_parent_session_id", "")),
            root_task_id=str(state.get("root_task_id", state["run_id"])),
            collaboration_snapshot_id=str(state.get("collaboration_snapshot_id", "")),
            business_operation_id=str(state.get("business_operation_id", "")),
            orchestration_owner=str(state.get("orchestration_owner", "agent")),
            workflow_id=str(state.get("workflow_id", "")),
            tenant_id=str(state["tenant_id"]),
            user_id=str(state["user_id"]),
            agent_id=str(state["agent_id"]),
            agent_version=str(state["agent_version"]),
            snapshot_id=str(state["snapshot_id"]),
            release_id=str(state.get("release_id", "")),
            release_stage=str(state.get("release_stage", "production")),
            release_projection_revision=int(state.get("release_projection_revision", 1)),
            traffic_policy_version=str(state.get("traffic_policy_version", "traffic-policy/v1")),
            side_effect_policy_version=str(state.get("side_effect_policy_version", "side-effect-policy/v1")),
            graph_version=str(state.get("graph_version", "")),
            model_policy_version=str(
                state.get("agent_snapshot", {}).get("model_policy_version", "")
            ),
            deadline_at=datetime.fromisoformat(str(state["deadline_at"])),
            attempt_budget_remaining=int(state.get("attempt_budget_remaining", 0)),
        )

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
        pinned_indexes = {
            str(item.get("index_version", ""))
            for item in knowledge
            if str(item.get("index_version", ""))
        }
        pinned_embeddings = {
            str(item.get("embedding_contract_id", ""))
            for item in knowledge
            if str(item.get("embedding_contract_id", ""))
        }
        if len(pinned_indexes) > 1 or len(pinned_embeddings) > 1:
            raise RuntimeLimitExceeded(
                "RAG_BINDING_AMBIGUOUS",
                "A single retrieval step cannot mix incompatible published index contracts.",
            )
        expected_index = next(iter(pinned_indexes), "")
        expected_embedding = next(iter(pinned_embeddings), "")
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
                            index_version=expected_index,
                            embedding_contract_id=expected_embedding,
                        )
                    )
                    if expected_index and rag_response.index_version != expected_index:
                        raise RuntimeLimitExceeded(
                            "RAG_INDEX_VERSION_DRIFT",
                            "RAG returned an index different from the published knowledge binding.",
                        )
                    if (
                        expected_embedding
                        and rag_response.embedding_contract_id != expected_embedding
                    ):
                        raise RuntimeLimitExceeded(
                            "RAG_EMBEDDING_CONTRACT_DRIFT",
                            "RAG returned evidence from a different embedding contract.",
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
        """先经过副作用屏障，再为确实可调度的工具预留预算。"""
        self._ensure_active(state)
        decision = AgentDecision.model_validate(state["decision"])
        if decision.action == AgentAction.SUBAGENT:
            budget = self.budget_guard.reserve_tool(self._budget(state))
            self._reserve_and_settle_root_tool_budget(state, decision, budget)
            return {"budget": budget.model_dump(mode="json"), "tool_deferred": False}
        binding = self.capability_evaluator.resolve(state, decision.tool_name)
        policy = self._tool_execution_policy(state, decision.tool_name)
        execution_id = deterministic_tool_execution_id(
            str(state.get("run_id", "")),
            self._step_id(state),
            decision.tool_name,
            decision.tool_arguments,
        )
        outcome = self.reference_monitor.evaluate(
            state,
            tool_name=decision.tool_name,
            binding=binding,
            policy=policy,
            tool_execution_id=execution_id,
        ).outcome
        if outcome == SideEffectBarrierOutcome.REPLAN_REQUIRED:
            self._record_session_event(
                state,
                RuntimeEventType.TOOL_DISPATCH_DEFERRED,
                {
                    "tool_name": decision.tool_name,
                    "tool_execution_id": execution_id,
                    "reason": outcome.value,
                },
            )
            return {
                "tool_deferred": True,
                "execution_trace": self._trace(state, "tool_deferred", {"reason": outcome.value}),
            }
        budget = self.budget_guard.reserve_tool(self._budget(state))
        self._reserve_and_settle_root_tool_budget(state, decision, budget)
        return {"budget": budget.model_dump(mode="json"), "tool_deferred": False}

    @staticmethod
    def _after_tool_guard(state: AgentState) -> str:
        """屏障要求重规划时跳过工具节点，避免已经过期的模型决定产生副作用。"""
        return "defer" if state.get("tool_deferred") else "tool"

    def _tool(self, state: AgentState) -> dict:
        """执行发布版本绑定的工具，并在重新提示模型前限制、脱敏不可信输出。

        工具待审批时图会持久化中断；拒绝直接终止，批准后只用一次性审批标识重试
        原调用。普通工具异常转为观察结果，供模型在剩余预算内处理。
        """
        self._ensure_active(state)
        decision = AgentDecision.model_validate(state["decision"])
        if decision.action == AgentAction.SUBAGENT:
            return self._subagent(state, decision)
        binding = self.capability_evaluator.resolve(state, decision.tool_name)
        published_version = str(binding["version"])
        step_id = self._step_id(state)
        tool_execution_id = deterministic_tool_execution_id(
            str(state.get("run_id", "")), step_id, decision.tool_name, decision.tool_arguments
        )
        tool_policy = self._tool_execution_policy(state, decision.tool_name)
        outcome = self.reference_monitor.evaluate(
            state,
            tool_name=decision.tool_name,
            binding=binding,
            policy=tool_policy,
            tool_execution_id=tool_execution_id,
        ).outcome
        if outcome == SideEffectBarrierOutcome.REPLAN_REQUIRED:
            self._record_session_event(
                state,
                RuntimeEventType.TOOL_DISPATCH_DEFERRED,
                {
                    "tool_name": decision.tool_name,
                    "tool_execution_id": tool_execution_id,
                    "reason": outcome.value,
                },
            )
            return {
                "tool_deferred": True,
                "execution_trace": self._trace(state, "tool_deferred", {"reason": outcome.value}),
            }
        self._record_session_event(
            state,
            RuntimeEventType.TOOL_INTENT_RECORDED,
            {
                "tool_name": decision.tool_name,
                "tool_version": published_version,
                "step_id": step_id,
                "tool_execution_id": tool_execution_id,
                "idempotency_key": tool_execution_id,
            },
            model_visible_message(
                "tool",
                str(bound_untrusted(decision.tool_arguments, 4_000)),
                source="runtime.tool.arguments",
                max_chars=4_000,
            ),
        )
        approval_ids = state.get("metadata", {}).get("tool_approval_ids", {})
        approval_id = (
            str(approval_ids.get(decision.tool_name, "")) if isinstance(approval_ids, dict) else ""
        )
        context = self._tool_context(
            state, approval_id, published_version, tool_execution_id=tool_execution_id
        )
        scheduled_call = tool_policy.scheduled_call(
            call_id=f"{state.get('run_id', '')}:{step_id}:{decision.tool_name}",
            tool_name=decision.tool_name,
        )
        try:
            self.interception_pipeline.apply(
                RuntimeHookPhase.PRE_TOOL_EXECUTE,
                self._hook_payload(
                    state, {"tool_name": decision.tool_name, "tool_version": published_version}
                ),
            )
            self._record_session_event(
                state,
                RuntimeEventType.TOOL_DISPATCHED,
                {
                    "tool_name": decision.tool_name,
                    "tool_version": published_version,
                    "step_id": step_id,
                    "tool_execution_id": tool_execution_id,
                    **self.tool_execution_engine.policy_facts(tool_policy),
                },
            )
            result = self.tool_execution_engine.execute(
                scheduled_call,
                lambda: self.tool_registry.execute(
                    decision.tool_name, decision.tool_arguments, context
                ),
            )
            if not (isinstance(result, dict) and result.get("status") == "PENDING_APPROVAL"):
                self._record_session_event(
                    state,
                    RuntimeEventType.TOOL_COMMITTED,
                    {
                        "tool_name": decision.tool_name,
                        "tool_version": published_version,
                        "step_id": step_id,
                        "tool_execution_id": tool_execution_id,
                    },
                )
            self._record_session_event(
                state,
                RuntimeEventType.TOOL_EXECUTION_OBSERVED,
                {
                    "tool_name": decision.tool_name,
                    "tool_version": published_version,
                    "step_id": step_id,
                    "tool_execution_id": tool_execution_id,
                    "status": "PENDING_APPROVAL"
                    if isinstance(result, dict) and result.get("status") == "PENDING_APPROVAL"
                    else "COMPLETED",
                },
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
                    self._record_session_event(
                        state,
                        RuntimeEventType.TOOL_RESULT,
                        {
                            "tool_name": decision.tool_name,
                            "success": False,
                            "status": "REJECTED",
                            "step_id": step_id,
                            "tool_execution_id": tool_execution_id,
                        },
                        model_visible_message(
                            "tool", str(observation), source="runtime.tool.rejected"
                        ),
                    )
                    self._record_session_event(
                        state,
                        RuntimeEventType.STEP_COMPLETED,
                        {"step_id": step_id, "outcome": "tool_rejected"},
                    )
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
                context = self._tool_context(
                    state, approved_id, published_version, tool_execution_id=tool_execution_id
                )
                self._record_session_event(
                    state,
                    RuntimeEventType.TOOL_DISPATCHED,
                    {
                        "tool_name": decision.tool_name,
                        "tool_version": published_version,
                        "step_id": step_id,
                        "tool_execution_id": tool_execution_id,
                        "approval_id": approved_id,
                        **self.tool_execution_engine.policy_facts(tool_policy),
                    },
                )
                result = self.tool_execution_engine.execute(
                    scheduled_call,
                    lambda: self.tool_registry.execute(
                        decision.tool_name,
                        decision.tool_arguments,
                        context,
                    ),
                )
                if not (isinstance(result, dict) and result.get("status") == "PENDING_APPROVAL"):
                    self._record_session_event(
                        state,
                        RuntimeEventType.TOOL_COMMITTED,
                        {
                            "tool_name": decision.tool_name,
                            "tool_version": published_version,
                            "step_id": step_id,
                            "tool_execution_id": tool_execution_id,
                            "approval_id": approved_id,
                        },
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
        self._record_session_event(
            state,
            RuntimeEventType.TOOL_RESULT,
            {
                "tool_name": decision.tool_name,
                "success": bool(observation["success"]),
                "step_id": step_id,
                "tool_execution_id": tool_execution_id,
            },
            model_visible_message(
                "tool",
                str(observation),
                source="runtime.tool.result",
                max_chars=int(state.get("metadata", {}).get("tool_result_max_chars", 12_000)),
            ),
        )
        self.interception_pipeline.apply(
            RuntimeHookPhase.POST_TOOL_RESULT,
            self._hook_payload(
                state, {"tool_name": decision.tool_name, "success": bool(observation["success"])}
            ),
        )
        self._record_session_event(
            state,
            RuntimeEventType.STEP_COMPLETED,
            {"step_id": step_id, "outcome": "tool"},
        )
        return {
            "observations": [*state.get("observations", []), observation],
            "workflow_cursor": self._workflow_after_side_effect(state, "tool"),
            "execution_trace": self._trace(
                state,
                "tool",
                {"tool": decision.tool_name, "success": observation["success"]},
            ),
        }

    def _subagent(self, state: AgentState, decision: AgentDecision) -> dict:
        """执行已发布子 Agent 委派；目标 Agent 仍经自身 Release/快照运行。"""
        self._ensure_active(state)
        if self.agent_manager is None:
            raise RuntimeLimitExceeded(
                "SUBAGENT_UNAVAILABLE", "Subagent execution is not deployed."
            )
        try:
            self._record_session_event(
                state,
                RuntimeEventType.SUBAGENT_DELEGATED,
                {
                    "target_agent_id": decision.subagent_id,
                    "capability": decision.subagent_capability,
                },
                model_visible_message(
                    "tool", decision.subagent_task, source="runtime.subagent.task", max_chars=4_000
                ),
            )
            if decision.subagent_capability:
                delegated = self.agent_manager.delegate_capability_group(
                    state,
                    requirement=CapabilityRequirement(capability_id=decision.subagent_capability),
                    task=decision.subagent_task,
                )
            else:
                delegation, result = self.agent_manager.delegate(
                    state,
                    target_agent_id=decision.subagent_id,
                    task=decision.subagent_task,
                )
                delegated = [(None, delegation, result)]
        except (SubAgentPolicyError, CollaborationError) as exc:
            raise RuntimeLimitExceeded("SUBAGENT_POLICY", str(exc)) from exc
        invocations = dict(state.get("subagent_invocations", {}))
        structured_results = []
        for selection, delegation, result in delegated:
            invocations[delegation.target_agent_id] = (
                invocations.get(delegation.target_agent_id, 0) + 1
            )
            structured_results.append(
                self.agent_manager.normalize_result(
                    result, selection=selection, agent_id=delegation.target_agent_id
                )
            )
        strategy = (
            delegated[0][0].binding.conflict_strategy
            if delegated[0][0] is not None
            else ConflictStrategy.AUTHORITY
        )
        resolved = ResultResolver().resolve(structured_results, strategy)
        if resolved is None:
            resume_value = interrupt(
                {
                    "type": "subagent_conflict",
                    "strategy": strategy.value,
                    "run_id": state.get("run_id", ""),
                    "candidates": [
                        {
                            "provider_agent_id": item.provider_agent_id,
                            "provider_snapshot_id": item.provider_snapshot_id,
                            "decision": item.decision,
                            "confidence": item.confidence,
                            "evidence_ids": item.evidence_ids,
                        }
                        for item in structured_results
                    ],
                }
            )
            resolution = ApprovalResume.model_validate(resume_value)
            if not resolution.approved or not resolution.selected_provider_agent_id:
                return {
                    "termination_reason": "SUBAGENT_CONFLICT_REJECTED",
                    "final_answer": "Conflicting expert results require an approved resolution.",
                }
            resolved = next(
                (
                    item
                    for item in structured_results
                    if item.provider_agent_id == resolution.selected_provider_agent_id
                ),
                None,
            )
            if resolved is None:
                raise RuntimeLimitExceeded(
                    "SUBAGENT_CONFLICT_SELECTION_INVALID",
                    "The selected conflict provider is not in the frozen candidate set.",
                )
        selected_index = next(
            index
            for index, item in enumerate(structured_results)
            if item.provider_agent_id == resolved.provider_agent_id
        )
        selection, delegation, result = delegated[selected_index]
        observation = {
            "type": "subagent",
            "agent_id": delegation.target_agent_id,
            "capability": decision.subagent_capability,
            "provider_selection": selection.reason
            if selection
            else "legacy_explicit_agent_binding",
            "success": result.get("status") == "COMPLETED",
            "agent_result": resolved.model_dump(mode="json"),
            "agent_results": [item.model_dump(mode="json") for item in structured_results],
            "conflict_strategy": strategy.value,
            "result": bound_untrusted(
                result, int(state.get("metadata", {}).get("tool_result_max_chars", 12_000))
            ),
        }
        self._record_session_event(
            state,
            RuntimeEventType.SUBAGENT_RESULT,
            {"target_agent_id": delegation.target_agent_id, "success": observation["success"]},
            model_visible_message(
                "tool", str(observation["result"]), source="runtime.subagent.result"
            ),
        )
        budget = self._budget(state)
        child_cost = sum(
            float(item_result.get("budget", {}).get("spent_cost_usd", 0))
            for _, _, item_result in delegated
        )
        budget = budget.model_copy(
            update={"spent_cost_usd": min(budget.max_cost_usd, budget.spent_cost_usd + child_cost)}
        )
        return {
            "subagent_invocations": invocations,
            "agent_results": [
                *state.get("agent_results", []),
                *(item.model_dump(mode="json") for item in structured_results),
            ],
            "observations": [*state.get("observations", []), observation],
            "budget": budget.model_dump(mode="json"),
            "workflow_cursor": self._workflow_after_side_effect(state, "tool"),
            "execution_trace": self._trace(
                state, "subagent", {"agent_id": delegation.target_agent_id}
            ),
        }

    def _tool_evidence(self, state: AgentState) -> dict:
        """把最新 Tool Observation 送入显式证据准入链路。

        顺序固定为 Parser → Schema → Security/ACL/Freshness → Extractor → Verifier
        → ExecutionState Store。失败只记录拒绝事实，既不进入 ``evidence``，也不会被
        Context Projection 交给 LLM。长期 RAG 摄取仍由 Artifact 审批流负责。
        """
        observations = state.get("observations", [])
        observation = observations[-1] if observations and isinstance(observations[-1], dict) else {}
        # 子 Agent 的回执由其自身 Runtime/Evidence 链路治理，不能伪装为本地工具证据。
        if observation.get("type") != "tool":
            return {}
        tool_name = str(observation.get("tool", ""))
        binding = self.capability_evaluator.resolve(state, tool_name)
        outcome = self.tool_evidence_pipeline.process(
            observation=observation,
            binding=binding,
            tenant_id=str(state.get("tenant_id", "")),
            user_id=str(state.get("user_id", "")),
            permissions=[str(item) for item in state.get("permissions", [])],
            run_id=str(state.get("run_id", "")),
            step_id=self._step_id(state),
        )
        record = outcome.record
        if outcome.evidence is None:
            self._record_session_event(
                state,
                RuntimeEventType.TOOL_EVIDENCE_REJECTED,
                {
                    "tool_name": tool_name,
                    "tool_version": record.get("tool_version", ""),
                    "reason": record.get("reason", "UNKNOWN"),
                    "step_id": self._step_id(state),
                },
            )
            return {
                "tool_evidence": [*state.get("tool_evidence", []), record],
                "execution_trace": self._trace(
                    state,
                    "tool_evidence_rejected",
                    {"tool": tool_name, "reason": record.get("reason", "UNKNOWN")},
                ),
            }
        self._record_session_event(
            state,
            RuntimeEventType.TOOL_EVIDENCE_STORED,
            {
                "tool_name": tool_name,
                "tool_version": record.get("tool_version", ""),
                "evidence_id": outcome.evidence["evidence_id"],
                "store": "runtime_execution_state",
                "persistence": "ephemeral",
                "step_id": self._step_id(state),
            },
        )
        return {
            "tool_evidence": [*state.get("tool_evidence", []), record],
            # Context Projection 由 PromptSecurityGuard 在下一次 Decision 中读取这个
            # state.evidence；未通过准入的 Observation 永远不会走这条分支。
            "evidence": [*state.get("evidence", []), outcome.evidence],
            "execution_trace": self._trace(
                state,
                "tool_evidence_stored",
                {"tool": tool_name, "evidence_id": outcome.evidence["evidence_id"]},
            ),
        }

    @staticmethod
    def _after_tool(state: AgentState) -> str:
        """按发布图决定工具后的安全去向，人工拒绝仍优先终止。"""
        if state.get("termination_reason") in {"TOOL_REJECTED", "SUBAGENT_CONFLICT_REJECTED"}:
            return "finish"
        if state.get("tool_deferred"):
            return "continue"
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
        raise RuntimeLimitExceeded(
            "WORKFLOW_CURSOR_INVALID", "Retrieval successor is not executable."
        )

    @staticmethod
    def _tool_context(
        state: AgentState,
        approval_id: str,
        tool_version: str,
        *,
        tool_execution_id: str = "",
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
            tool_execution_id=tool_execution_id,
            root_task_id=str(state.get("root_task_id", "")),
            business_operation_id=str(state.get("business_operation_id", "")),
            operation_id=tool_execution_id,
            step_id=AgentGraph._step_id(state, pending=True),
            plan_id=str(state.get("execution_plan", {}).get("plan_id", "")),
            plan_admission_id=str(state.get("plan_admission", {}).get("admission_id", "")),
            idempotency_key=tool_execution_id,
            trace_id=state.get("trace_id", ""),
            run_id=state.get("run_id", ""),
            session_id=state.get("session_id", ""),
            agent_id=state.get("agent_id", ""),
            agent_version=state.get("agent_version", ""),
            snapshot_id=state.get("snapshot_id", ""),
            release_id=state.get("release_id", ""),
            release_stage=state.get("release_stage", "production"),
            release_projection_revision=int(state.get("release_projection_revision", 1)),
            traffic_policy_version=state.get("traffic_policy_version", "traffic-policy/v1"),
            side_effect_policy_version=state.get("side_effect_policy_version", "side-effect-policy/v1"),
            deadline_at=state.get("deadline_at", ""),
            attempt_budget_remaining=AgentGraph._budget(state).remaining_attempts,
            tool_version=tool_version,
        )

    @staticmethod
    def _tool_execution_policy(state: AgentState, tool_name: str) -> ToolExecutionPolicy:
        """由冻结工具绑定解析副作用策略，模型不能覆盖调度、审批或资源键。"""
        binding = ToolCapabilityEvaluator.resolve(state, tool_name)
        return ToolExecutionPolicy.from_published_binding(
            binding,
            tenant_id=str(state.get("tenant_id", "")),
            tool_name=tool_name,
        )

    def _clarify(self, state: AgentState) -> dict:
        """以可恢复中断请求澄清；恢复载荷只能是邮箱租约，不把正文写入 Graph。"""
        resume_value = interrupt(
            {
                "type": "user_input",
                "message": "Please clarify the request so that its intent can be determined safely.",
                "run_id": state.get("run_id", ""),
            }
        )
        input_resume = UserInputResume.model_validate(resume_value)
        return {
            "mailbox_replan": True,
            "mailbox_message_id": input_resume.message_id,
            "mailbox_lease_token": input_resume.lease_token,
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
                self._record_session_event(
                    state,
                    RuntimeEventType.STEP_COMPLETED,
                    {"step_id": self._step_id(state), "outcome": "max_steps"},
                )
                return {
                    "final_answer": "Unable to complete within the configured agent step budget.",
                    "termination_reason": "MAX_STEPS",
                }
            validate_final_output(state.get("compiled_plan", {}), decision.final_answer)
            self._record_session_event(
                state,
                RuntimeEventType.STEP_COMPLETED,
                {"step_id": self._step_id(state), "outcome": "answer"},
            )
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
            output_findings = self.prompt_security.inspect_output(answer)
            if output_findings:
                self._record_session_event(
                    state,
                    RuntimeEventType.STEP_COMPLETED,
                    {
                        "step_id": self._step_id(state),
                        "outcome": "prompt_security_blocked_output",
                        "finding_codes": [item.code for item in output_findings],
                    },
                )
                return {
                    "final_answer": "The response was withheld by the prompt-security policy.",
                    "safety_status": "BLOCKED_PROMPT_SECURITY",
                    "termination_reason": "PROMPT_SECURITY_BLOCKED_OUTPUT",
                }
            return {"final_answer": answer, "safety_status": "PASSED"}

    def _result(self, result: dict[str, Any]) -> AgentRunResult:
        """将 LangGraph 内部状态压缩为公开 API 结果，并保留审批中断详情。"""
        interrupt_items = [item.value for item in result.get("__interrupt__", [])]
        if interrupt_items:
            user_input = next(
                (
                    item
                    for item in interrupt_items
                    if isinstance(item, dict) and item.get("type") == "user_input"
                ),
                None,
            )
            if user_input is not None:
                return AgentRunResult(
                    status="WAITING_INPUT",
                    answer=str(user_input.get("message", "Please provide additional input.")),
                    steps=result.get("step_count", 0),
                    termination_reason="USER_INPUT_REQUIRED",
                    evidence=result.get("evidence", []),
                    observations=result.get("observations", []),
                    tool_evidence=result.get("tool_evidence", []),
                    execution_plan=result.get("execution_plan", {}),
                    budget=result.get("budget", {}),
                    execution_trace=result.get("execution_trace", []),
                    interrupts=interrupt_items,
                    context_summary=self._context_summary(result),
                )
            return AgentRunResult(
                status="WAITING_APPROVAL",
                answer="",
                steps=result.get("step_count", 0),
                termination_reason="HUMAN_APPROVAL_REQUIRED",
                evidence=result.get("evidence", []),
                observations=result.get("observations", []),
                tool_evidence=result.get("tool_evidence", []),
                execution_plan=result.get("execution_plan", {}),
                budget=result.get("budget", {}),
                execution_trace=result.get("execution_trace", []),
                interrupts=interrupt_items,
                context_summary=self._context_summary(result),
            )
        return AgentRunResult(
            status="COMPLETED",
            answer=result.get("final_answer", ""),
            steps=result.get("step_count", 0),
            termination_reason=result.get("termination_reason", "ANSWERED"),
            evidence=result.get("evidence", []),
            observations=result.get("observations", []),
            tool_evidence=result.get("tool_evidence", []),
            execution_plan=result.get("execution_plan", {}),
            budget=result.get("budget", {}),
            execution_trace=result.get("execution_trace", []),
            context_summary=self._context_summary(result),
        )

    @staticmethod
    def _context_summary(result: dict[str, Any]) -> dict[str, Any]:
        """输出可审查但不泄露全文的 Context 选择清单，证明哪些历史进入了决策。"""
        messages: list[dict[str, Any]] = []
        for item in result.get("conversation_history", []):
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", ""))
            metadata = item.get("metadata", {})
            messages.append({
                "role": item.get("role", "unknown"),
                "created_at": item.get("created_at", ""),
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "source": metadata.get("source", "context-service")
                if isinstance(metadata, dict) else "context-service",
            })
        return {
            "selected_history": messages,
            "selected_history_count": len(messages),
            "status": result.get("context_status", {}),
        }

    @staticmethod
    def _limited_result(state: dict[str, Any], error: RuntimeLimitExceeded) -> AgentRunResult:
        """将预算或发布边界错误标准化为无敏感细节的 ``LIMIT_EXCEEDED`` 响应。"""
        explanations = {
            "MAX_LLM_CALLS": "模型调用次数已达到当前 Release 上限。未能生成最终答案。",
            "MAX_TOOL_CALLS": "工具调用次数已达到当前 Release 上限。任务未能继续。",
            "MAX_RETRIEVAL_ROUNDS": "检索轮次已达到当前 Release 上限。任务未能继续。",
            "ATTEMPT_BUDGET_EXCEEDED": "下游总尝试额度已耗尽。任务未能继续。",
            "DEADLINE_EXCEEDED": "任务超过当前 Release 的执行时限。",
            "COST_BUDGET_EXCEEDED": "任务达到当前 Release 的成本上限。",
            "MAX_STEPS": "Agent 步数已达到当前 Release 上限。",
        }
        return AgentRunResult(
            status="LIMIT_EXCEEDED",
            answer=explanations.get(
                error.code,
                f"任务被已发布的运行策略终止 ({error.code})。",
            ),
            steps=state.get("step_count", 0),
            termination_reason=error.code,
            evidence=state.get("evidence", []),
            observations=state.get("observations", []),
            tool_evidence=state.get("tool_evidence", []),
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
            tool_evidence=state.get("tool_evidence", []),
            execution_plan=state.get("execution_plan", {}),
            budget=state.get("budget", {}),
            execution_trace=state.get("execution_trace", []),
        )

    def _ensure_active(self, state: AgentState) -> None:
        """检查时间、成本、调用次数及外部取消标记；失败即阻止下一副作用。"""
        self.stop_policy.enforce(state)

    def _record_session_event(
        self,
        state: AgentState,
        event_type: RuntimeEventType,
        metadata: dict[str, Any],
        model_message: ModelVisibleMessage | None = None,
    ) -> None:
        """记录已形成的运行事实；审计写入失败必须中断而不是制造不可解释执行。"""
        if self.session_event_recorder is not None:
            self.session_event_recorder(state, event_type, metadata, model_message)

    @staticmethod
    def _step_id(state: AgentState, *, pending: bool = False) -> str:
        """生成稳定 Step 标识；同一 LangGraph 检查点重放时必须复用同一副作用键。"""
        ordinal = int(state.get("step_count", 0)) + (1 if pending else 0)
        return f"step_{state.get('run_id', '')}_{max(1, ordinal)}"

    @staticmethod
    def _hook_payload(state: AgentState, extra: dict[str, Any]) -> dict[str, Any]:
        """为 Hook 提供最小且身份受保护的上下文，不暴露未裁剪的证据或工具正文。"""
        return {
            "tenant_id": state.get("tenant_id", ""),
            "user_id": state.get("user_id", ""),
            "run_id": state.get("run_id", ""),
            "trace_id": state.get("trace_id", ""),
            "snapshot_id": state.get("snapshot_id", ""),
            "agent_id": state.get("agent_id", ""),
            **extra,
        }

    @staticmethod
    def _epoch_hash(value: object) -> str:
        """对模型可见输入生成稳定摘要，账本可证明上下文版本但不复制敏感正文。"""
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

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
            AgentAction.SUBAGENT: "tool",
            AgentAction.ANSWER: "answer",
        }[action]
        if (
            role == "answer"
            and action == AgentAction.ANSWER
            and cursor in workflow.get("terminals", [])
        ):
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
    def _workflow_next_capability_node(
        state: AgentState, provider_kind: CapabilityProviderKind
    ) -> str:
        """在 Resolver 确定 Provider 类型后才推进发布 Graph，避免模型预选路径。"""
        workflow = state.get("compiled_plan", {}).get("workflow_policy", {})
        if not workflow or workflow.get("local_development_only"):
            return state.get("workflow_cursor", "")
        cursor = str(state.get("workflow_cursor") or workflow.get("entrypoint") or "")
        roles = workflow.get("node_roles", {})
        if roles.get(cursor) != "decision":
            raise RuntimeLimitExceeded(
                "WORKFLOW_DECISION_FORBIDDEN",
                f"Published workflow node '{cursor}' cannot resolve a capability.",
            )
        requested_role = "retrieval" if provider_kind == CapabilityProviderKind.RAG else "tool"
        facts = AgentGraph._workflow_facts(state, action=AgentAction.CAPABILITY)
        matches = [
            item["to"]
            for item in workflow.get("adjacency", {}).get(cursor, [])
            if roles.get(item["to"]) == requested_role
            and evaluate_workflow_condition(item.get("condition"), facts)
        ]
        if len(matches) != 1:
            raise RuntimeLimitExceeded(
                "WORKFLOW_ACTION_FORBIDDEN",
                f"Published workflow does not allow capability provider {provider_kind.value}.",
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

    def _reserve_and_settle_root_tool_budget(
        self, state: AgentState, decision: AgentDecision, budget: RuntimeBudget
    ) -> None:
        """把工具保守成本结算到共享账本；真实工具计费目前由固定预留表示。

        Tool Gateway 尚未对每种工具统一返回成本字段，因此这里按已发布的工具调用
        预留结算，不能把未知价格伪装成零成本。未来 Gateway 返回实际成本后，可在
        此处替换为对应计费值而不改变账本事务语义。
        """
        del budget  # 本地预算已由调用方更新；共享账本只需要本次固定预留。
        reservation_id = self._reserve_root_budget(
            state,
            f"tool:{self._step_id(state)}:{decision.tool_name or decision.subagent_capability}",
            cost_usd=self.budget_guard.tool_call_reservation_usd,
            steps=0,
        )
        self._settle_root_budget(
            reservation_id,
            actual_cost_usd=self.budget_guard.tool_call_reservation_usd,
            actual_steps=0,
        )

    def _reserve_root_budget(
        self, state: AgentState, phase: str, *, cost_usd: float, steps: int
    ) -> str:
        """为一次外部动作在 Root Task 账本中预留额度；无账本的单元测试显式跳过。"""
        ledger = self.runtime_context.session
        if ledger is None or not hasattr(ledger, "reserve_root_budget"):
            return ""
        tenant_id = str(state.get("tenant_id", ""))
        root_task_id = str(state.get("root_task_id") or state.get("run_id", ""))
        run_id = str(state.get("run_id", ""))
        if not tenant_id or not root_task_id or not run_id:
            # 纯内存 Graph 仍可用于本地测试；生产 API 必定填充这三项关联 ID。
            return ""
        reservation_id = (
            "rbr_"
            + hashlib.sha256(f"{tenant_id}:{root_task_id}:{run_id}:{phase}".encode()).hexdigest()[
                :32
            ]
        )
        ledger.reserve_root_budget(
            tenant_id,
            root_task_id,
            run_id,
            reservation_id,
            cost_usd=cost_usd,
            steps=steps,
        )
        return reservation_id

    def _settle_root_budget(
        self, reservation_id: str, *, actual_cost_usd: float, actual_steps: int
    ) -> None:
        """结算已创建的共享预留；空 ID 表示该运行不具备持久账本能力。"""
        if not reservation_id:
            return
        ledger = self.runtime_context.session
        if ledger is not None and hasattr(ledger, "settle_root_budget"):
            ledger.settle_root_budget(
                reservation_id,
                actual_cost_usd=actual_cost_usd,
                actual_steps=actual_steps,
            )

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

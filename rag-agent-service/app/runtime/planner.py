from __future__ import annotations

import json
import re
from typing import Any, Protocol
from uuid import uuid4

from app.infrastructure.llm_gateway_client import LlmGatewayClient
from app.runtime.models import (
    ComplexityAssessment,
    CostAssessment,
    EntityResult,
    ExecutionPlan,
    IntentResult,
    RouteDecision,
    RouteType,
    RuntimeBudget,
    SlaAssessment,
    SourcePlan,
)
from app.runtime.retrieval_policy import infer_profile, resolve_profile

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_IDENTIFIER = re.compile(
    r"\b(?:ORD|DOC|CASE|TICKET|RUN)-[A-Za-z0-9-]{3,}\b",
    re.IGNORECASE,
)
_AMOUNT = re.compile(r"(?:¥|￥|\$)\s?\d+(?:\.\d{1,2})?")


class SemanticAnalysis(Protocol):
    uses_llm: bool

    def analyze(
        self, state: dict[str, Any]
    ) -> tuple[IntentResult, list[EntityResult], SourcePlan]: ...


class HeuristicSemanticAnalyzer:
    uses_llm = False

    def analyze(
        self, state: dict[str, Any]
    ) -> tuple[IntentResult, list[EntityResult], SourcePlan]:
        task = state["task"]
        lowered = task.lower()
        intent_name, confidence, reason = self._intent(lowered)
        entities = self._entities(task)
        metadata = state.get("metadata", {})
        snapshot = state.get("agent_snapshot", {})
        spec = snapshot.get("spec", {}) if isinstance(snapshot, dict) else {}
        knowledge = [
            str(item.get("knowledge_base"))
            for item in spec.get("knowledge", [])
            if item.get("knowledge_base")
        ]
        requested = metadata.get("required_sources", [])
        if isinstance(requested, list):
            knowledge = list(
                dict.fromkeys([*knowledge, *(str(item) for item in requested)])
            )
        context_sources: list[str] = []
        permissions = ["rag:read"] if knowledge else []
        if intent_name == "refund_application":
            context_sources.append("order-service")
            permissions.append("order:read")
        if intent_name == "tool_operation":
            context_sources.append("tool-gateway")
        source_plan = SourcePlan(
            knowledge_bases=knowledge,
            context_sources=context_sources,
            required_permissions=list(dict.fromkeys(permissions)),
            reason="Sources are derived from the published snapshot, intent, and request metadata.",
        )
        return (
            IntentResult(name=intent_name, confidence=confidence, reason=reason),
            entities,
            source_plan,
        )

    @staticmethod
    def _intent(lowered: str) -> tuple[str, float, str]:
        rules = [
            (("refund", "退款", "退货"), "refund_application"),
            (("audit", "review", "审查", "审核", "合规"), "compliance_review"),
            (
                ("create", "update", "delete", "execute", "创建", "更新", "执行"),
                "tool_operation",
            ),
            (("find", "search", "query", "查询", "检索", "查找"), "knowledge_query"),
        ]
        for words, intent in rules:
            if any(word in lowered for word in words):
                return (
                    intent,
                    0.86,
                    f"Matched deterministic intent vocabulary for {intent}.",
                )
        return "general_question", 0.62, "No specialized intent rule matched."

    @staticmethod
    def _entities(task: str) -> list[EntityResult]:
        found: list[EntityResult] = []
        for name, pattern in (
            ("email", _EMAIL),
            ("date", _DATE),
            ("business_id", _IDENTIFIER),
            ("amount", _AMOUNT),
        ):
            found.extend(
                EntityResult(name=name, value=value) for value in pattern.findall(task)
            )
        return found


class GatewaySemanticAnalyzer:
    uses_llm = True
    _SYSTEM = """Analyze an enterprise agent request. Return one JSON object with:
intent{name,confidence,reason}, entities[{name,value,confidence}], and
source_plan{knowledge_bases,context_sources,required_permissions,reason}.
Only select knowledge bases present in the published snapshot. Never invent permissions."""

    def __init__(self, gateway: LlmGatewayClient, model: str) -> None:
        self.gateway = gateway
        self.model = model

    def analyze(
        self, state: dict[str, Any]
    ) -> tuple[IntentResult, list[EntityResult], SourcePlan]:
        payload = {
            "task": state["task"],
            "metadata": state.get("metadata", {}),
            "published_spec": state.get("agent_snapshot", {}).get("spec", {}),
            "conversation_history": state.get("conversation_history", [])[-12:],
            "user_context": state.get("user_context", {}),
            "context_status": state.get("context_status", {}),
            "published_execution_contract": {
                "graph_execution_order": state.get("compiled_plan", {}).get(
                    "graph_execution_order", []
                ),
                "graph_node_kinds": state.get("compiled_plan", {}).get(
                    "graph_node_kinds", {}
                ),
                "data_region": state.get("compiled_plan", {}).get("data_region"),
            },
        }
        result = self.gateway.complete_json(
            select_logical_model(
                state.get("agent_snapshot", {}),
                self.model,
                state.get("compiled_plan", {}),
            ),
            self._SYSTEM,
            json.dumps(payload, ensure_ascii=False),
            execution_headers=_execution_headers(state),
        )
        return (
            IntentResult.model_validate(result["intent"]),
            [EntityResult.model_validate(item) for item in result.get("entities", [])],
            SourcePlan.model_validate(result.get("source_plan", {})),
        )

    def last_cost_usd(self) -> float | None:
        return self.gateway.last_cost_usd()


class RuntimePlanner:
    def __init__(self, analyzer: SemanticAnalysis) -> None:
        self.analyzer = analyzer

    def analyze(self, state: dict[str, Any]) -> dict[str, Any]:
        intent, entities, source_plan = self.analyzer.analyze(state)
        return {
            "intent": intent.model_dump(mode="json"),
            "entities": [item.model_dump(mode="json") for item in entities],
            "source_plan": source_plan.model_dump(mode="json"),
            "profile_decision": infer_profile(
                state["task"], intent.name, state.get("metadata", {})
            ).model_dump(mode="json"),
        }

    def build_plan(self, state: dict[str, Any]) -> ExecutionPlan:
        intent = IntentResult.model_validate(state["intent"])
        entities = [
            EntityResult.model_validate(item) for item in state.get("entities", [])
        ]
        sources = SourcePlan.model_validate(state["source_plan"])
        budget = RuntimeBudget.model_validate(state["budget"])
        complexity = _complexity(state, intent, entities, sources)
        sla = _sla(budget, complexity)
        estimated_cost = _estimated_cost(complexity, sources)
        cost = CostAssessment(
            max_cost_usd=budget.max_cost_usd,
            estimated_cost_usd=estimated_cost,
            remaining_cost_usd=budget.remaining_cost_usd,
            feasible=estimated_cost <= budget.remaining_cost_usd,
        )
        profile_decision = infer_profile(
            state["task"], intent.name, state.get("metadata", {})
        )
        effective_policy = resolve_profile(
            profile_decision,
            snapshot=state.get("agent_snapshot", {}),
            budget=state.get("budget", {}),
            metadata=state.get("metadata", {}),
        )
        route = _route(intent, sources, complexity, sla, cost)
        snapshot = state.get("agent_snapshot", {})
        return ExecutionPlan(
            plan_id=f"plan_{uuid4().hex}",
            intent=intent,
            entities=entities,
            source_plan=sources,
            complexity=complexity,
            sla=sla,
            cost=cost,
            route=route,
            agent_version=state.get("agent_version", "local-unversioned"),
            graph_version=snapshot.get("graph_version", "runtime-planner-v1"),
            model_policy_version=snapshot.get(
                "model_policy_version", "local-unversioned"
            ),
            retrieval_policy=effective_policy.model_dump(mode="json"),
        )


def _complexity(
    state: dict[str, Any],
    intent: IntentResult,
    entities: list[EntityResult],
    sources: SourcePlan,
) -> ComplexityAssessment:
    score = 5
    reasons: list[str] = []

    def add(points: int, reason: str) -> None:
        nonlocal score
        score += points
        reasons.append(reason)

    if sources.knowledge_bases:
        add(10, "requires_private_knowledge")
    source_count = len(sources.knowledge_bases) + len(sources.context_sources)
    if source_count > 1:
        add(min(24, (source_count - 1) * 8), "multiple_sources")
    if intent.name == "tool_operation":
        add(15, "requires_tool")
    if state.get("metadata", {}).get("write_operation"):
        add(20, "write_operation")
    if state.get("metadata", {}).get("approval_required"):
        add(20, "human_approval")
    if intent.confidence < 0.7:
        add(15, "low_intent_confidence")
    if len(entities) >= 3:
        add(10, "multiple_entities")
    if len(state.get("task", "")) > 2_000:
        add(10, "long_request")
    score = min(100, score)
    level = (
        "simple"
        if score < 30
        else "medium"
        if score < 60
        else "complex"
        if score < 80
        else "critical"
    )
    return ComplexityAssessment(score=score, level=level, reasons=reasons)


def _sla(budget: RuntimeBudget, complexity: ComplexityAssessment) -> SlaAssessment:
    minimum = (
        1_000
        if complexity.level == "simple"
        else 3_000
        if complexity.level == "medium"
        else 8_000
    )
    tier = (
        "realtime"
        if budget.remaining_ms < 3_000
        else "interactive"
        if budget.remaining_ms < 30_000
        else "standard"
    )
    return SlaAssessment(
        deadline_at=budget.deadline_at,
        remaining_ms=budget.remaining_ms,
        tier=tier,
        feasible=budget.remaining_ms >= minimum,
    )


def _estimated_cost(complexity: ComplexityAssessment, sources: SourcePlan) -> float:
    base = (
        0.01
        if complexity.level == "simple"
        else 0.03
        if complexity.level == "medium"
        else 0.08
    )
    return round(
        base + 0.002 * (len(sources.knowledge_bases) + len(sources.context_sources)), 6
    )


def _route(
    intent: IntentResult,
    sources: SourcePlan,
    complexity: ComplexityAssessment,
    sla: SlaAssessment,
    cost: CostAssessment,
) -> RouteDecision:
    reasons: list[str] = []
    if intent.confidence < 0.6:
        route = RouteType.CLARIFY
        reasons.append("intent_confidence_below_threshold")
    elif not sla.feasible:
        route = RouteType.DIRECT if not sources.knowledge_bases else RouteType.RAG
        reasons.append("sla_requires_fast_path")
    elif not cost.feasible:
        route = RouteType.RAG if sources.knowledge_bases else RouteType.DIRECT
        reasons.append("cost_requires_fallback")
    elif intent.name == "tool_operation":
        route = RouteType.TOOL
        reasons.append("intent_requires_tool")
    elif complexity.score >= 60:
        route = RouteType.DEEP_REASONING
        reasons.append("complex_request")
    elif sources.knowledge_bases or sources.context_sources:
        route = RouteType.RAG
        reasons.append("request_requires_sources")
    else:
        route = RouteType.DIRECT
        reasons.append("simple_request_without_external_sources")
    fallbacks = {
        RouteType.DEEP_REASONING: [RouteType.RAG, RouteType.DIRECT],
        RouteType.TOOL: [RouteType.RAG, RouteType.CLARIFY],
        RouteType.RAG: [RouteType.DIRECT, RouteType.CLARIFY],
        RouteType.DIRECT: [RouteType.CLARIFY],
        RouteType.CLARIFY: [],
    }[route]
    quality = (
        "high"
        if route == RouteType.DEEP_REASONING
        else "balanced"
        if route in {RouteType.RAG, RouteType.TOOL}
        else "fast"
    )
    return RouteDecision(
        route=route, quality_tier=quality, reasons=reasons, fallback_chain=fallbacks
    )


def _execution_headers(state: dict[str, Any]) -> dict[str, str]:
    budget = state.get("budget", {})
    return {
        "X-Tenant-Id": state["tenant_id"],
        "X-User-Id": state["user_id"],
        "X-Request-Id": state["request_id"],
        "X-Trace-Id": state.get("trace_id", state["request_id"]),
        "X-Run-Id": state.get("run_id", ""),
        "X-Agent-Id": state.get("agent_id", ""),
        "X-Agent-Version": state.get("agent_version", ""),
        "X-Snapshot-Id": state.get("snapshot_id", ""),
        "X-Deadline-At": state.get("deadline_at", ""),
        "X-Attempt-Budget-Remaining": str(
            state.get("budget", {}).get("max_attempts", 0)
            - state.get("budget", {}).get("attempts_used", 0)
        ),
        "X-Cost-Budget": str(
            max(
                0.0,
                float(budget.get("max_cost_usd", 0))
                - float(budget.get("spent_cost_usd", 0)),
            )
        ),
        "X-Data-Region": str(
            state.get("compiled_plan", {}).get("data_region") or "unspecified"
        ),
    }


def select_logical_model(
    snapshot: dict[str, Any],
    fallback: str,
    compiled_plan: dict[str, Any] | None = None,
) -> str:
    compiled = compiled_plan or {}
    if compiled.get("logical_model"):
        return str(compiled["logical_model"])
    spec = snapshot.get("spec", {}) if isinstance(snapshot, dict) else {}
    policy = spec.get("model_policy", {}) if isinstance(spec, dict) else {}
    default_route = policy.get("default_route")
    for route in policy.get("routes", []):
        if route.get("route_name") == default_route and route.get("models"):
            return str(route["models"][0])
    return fallback

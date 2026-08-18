"""Deterministic request analysis and route selection before model execution."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Protocol
from uuid import uuid4

from platform_sdk.clients.llm_gateway import LlmGatewayClient
from platform_sdk.contracts.execution_profile import legacy_execution_mode

from agent_runtime_service.runtime.intent_catalog import (
    DEFAULT_INTENT_CATALOG,
    IntentCatalog,
    resolve_catalog,
)
from agent_runtime_service.runtime.models import (
    ComplexityAssessment,
    CostAssessment,
    EntityResult,
    ExecutionMode,
    ExecutionPlan,
    ExecutionRequirements,
    IntentResult,
    RouteDecision,
    RouteType,
    RuntimeBudget,
    SlaAssessment,
    SourcePlan,
)
from agent_runtime_service.runtime.prompt_security import PromptSecurityGuard
from agent_runtime_service.runtime.retrieval_policy import infer_profile, resolve_profile

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_IDENTIFIER = re.compile(
    r"\b(?:ORD|DOC|CASE|TICKET|RUN)-[A-Za-z0-9-]{3,}\b",
    re.IGNORECASE,
)
_AMOUNT = re.compile(r"(?:¥|￥|\$)\s?\d+(?:\.\d{1,2})?")


class SemanticAnalysis(Protocol):
    """Expose optional semantic enrichment without changing planner contracts."""

    uses_llm: bool

    def analyze(
        self, state: dict[str, Any]
    ) -> tuple[IntentResult, list[EntityResult], SourcePlan]:
        """从运行状态提取意图、实体和允许的数据源，不生成最终执行动作。"""
        ...


class HeuristicSemanticAnalyzer:
    """Local deterministic analyzer used when no semantic model is available."""

    uses_llm = False

    def __init__(self, catalog: IntentCatalog = DEFAULT_INTENT_CATALOG) -> None:
        """注入冻结意图目录，使规则命中可独立于模型发布、测试和审计演进。"""
        self.catalog = catalog

    def analyze(self, state: dict[str, Any]) -> tuple[IntentResult, list[EntityResult], SourcePlan]:
        """用可解释词表和发布绑定生成本地分析，作为网关不可用时的确定性路径。"""
        task = state["task"]
        compiled = state.get("compiled_plan", {})
        catalog = resolve_catalog(compiled) if compiled else self.catalog
        intent = catalog.resolve(task)
        intent_name = intent.name
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
            knowledge = list(dict.fromkeys([*knowledge, *(str(item) for item in requested)]))
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
            intent,
            entities,
            source_plan,
        )

    @staticmethod
    def _entities(task: str) -> list[EntityResult]:
        """提取有限结构化实体供规划使用，不改写或持久化原始请求。"""
        found: list[EntityResult] = []
        for name, pattern in (
            ("email", _EMAIL),
            ("date", _DATE),
            ("business_id", _IDENTIFIER),
            ("amount", _AMOUNT),
        ):
            found.extend(EntityResult(name=name, value=value) for value in pattern.findall(task))
        return found


class GatewaySemanticAnalyzer:
    """Use a governed LLM Gateway analysis endpoint as an optional enrichment."""

    uses_llm = True
    _SYSTEM = """Analyze an enterprise agent request. Return one JSON object with:
intent{name,confidence,reason}, entities[{name,value,confidence}], and
source_plan{knowledge_bases,context_sources,required_permissions,reason}.
Only select knowledge bases present in the published snapshot. Never invent permissions."""

    def __init__(self, gateway: LlmGatewayClient, model: str) -> None:
        """注入受治理网关和逻辑模型名，使语义增强仍经过既定模型路由。"""
        self.gateway = gateway
        self.model = model
        self.prompt_security = PromptSecurityGuard()

    def analyze(self, state: dict[str, Any]) -> tuple[IntentResult, list[EntityResult], SourcePlan]:
        """向网关请求结构化语义分析，同时只暴露快照允许的来源范围。"""
        compiled = state.get("compiled_plan", {})
        catalog = resolve_catalog(compiled) if compiled else DEFAULT_INTENT_CATALOG
        untrusted_segments, findings = self.prompt_security.prepare_model_input(state)
        payload = {
            "task": state["task"],
            "metadata": state.get("metadata", {}),
            "published_spec": state.get("agent_snapshot", {}).get("spec", {}),
            "conversation_history": untrusted_segments["untrusted_history"],
            "untrusted_evidence": untrusted_segments["untrusted_evidence"],
            "untrusted_tool_observations": untrusted_segments["untrusted_tool_observations"],
            "user_context": state.get("user_context", {}),
            "context_status": state.get("context_status", {}),
            # 语义模型只能在发布目录列出的意图空间中输出；目录正文也会进入 Plan 指纹。
            "intent_catalog": {
                "version": catalog.version,
                "definitions": [
                    {
                        "name": item.name,
                        "domain": item.domain,
                        "action": item.action,
                        "required_entities": list(item.required_entities),
                    }
                    for item in catalog.definitions
                ],
            },
            "prompt_security": {"finding_codes": sorted({item.code for item in findings})},
            "published_execution_contract": {
                "graph_execution_order": state.get("compiled_plan", {}).get(
                    "graph_execution_order", []
                ),
                "graph_node_kinds": state.get("compiled_plan", {}).get("graph_node_kinds", {}),
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
        """读取本次分析对应的网关账单，供 Graph 用实际费用对账。"""
        return self.gateway.last_cost_usd()


class RuntimePlanner:
    """Translate request signals into bounded planning metadata, not final actions."""

    def __init__(self, analyzer: SemanticAnalysis) -> None:
        """注入可替换的语义分析器，便于在网关故障时切换到确定性分析。"""
        self.analyzer = analyzer

    def analyze(self, state: dict[str, Any]) -> dict[str, Any]:
        """序列化分析输出与候选检索档位，尚不允许其直接决定执行动作。"""
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
        """把分析、预算和发布快照合成为可审计的有限执行计划。"""
        intent = IntentResult.model_validate(state["intent"])
        entities = [EntityResult.model_validate(item) for item in state.get("entities", [])]
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
        profile_decision = infer_profile(state["task"], intent.name, state.get("metadata", {}))
        effective_policy = resolve_profile(
            profile_decision,
            snapshot=state.get("agent_snapshot", {}),
            budget=state.get("budget", {}),
            metadata=state.get("metadata", {}),
        )
        route = _route(intent, sources, complexity, sla, cost)
        snapshot = state.get("agent_snapshot", {})
        analyzer_version = type(self.analyzer).__name__
        compiled = state.get("compiled_plan", {})
        input_fingerprint = _fingerprint(
            {
                "task": state.get("task", ""),
                "metadata": state.get("metadata", {}),
                "snapshot_id": state.get("snapshot_id", ""),
                "compiled_contract_hash": compiled.get("contract_hash", ""),
            }
        )
        policy_fingerprint = _fingerprint(
            {
                "workflow": compiled.get("workflow_policy", {}),
                "intent_catalog": compiled.get("intent_catalog"),
                "retrieval": effective_policy.model_dump(mode="json"),
                "permissions": sorted(state.get("permissions", [])),
                "budget_limits": budget.model_dump(mode="json"),
            }
        )
        plan_payload = {
            "intent": intent.model_dump(mode="json"),
            "entities": [item.model_dump(mode="json") for item in entities],
            "source_plan": sources.model_dump(mode="json"),
            "complexity": complexity.model_dump(mode="json"),
            "sla": sla.model_dump(mode="json"),
            "cost": cost.model_dump(mode="json"),
            "route": route.model_dump(mode="json"),
            "agent_version": state.get("agent_version", "local-unversioned"),
            "graph_version": snapshot.get("graph_version", "runtime-planner-v1"),
            "model_policy_version": snapshot.get("model_policy_version", "local-unversioned"),
            "executor_profile": compiled.get("executor_profile", "local-default/v1"),
            "execution_mode": _execution_mode(
                str(compiled.get("executor_profile", "local-default/v1"))
            ),
            "execution_requirements": ExecutionRequirements.model_validate(
                compiled.get("execution_requirements", {})
            ).model_dump(mode="json"),
            "intent_catalog_version": str(compiled.get("intent_catalog_version", "platform-default/v1")),
            "retrieval_policy": effective_policy.model_dump(mode="json"),
            "planner_version": "runtime-planner/v2",
            "analyzer_version": analyzer_version,
            "input_fingerprint": input_fingerprint,
            "policy_fingerprint": policy_fingerprint,
        }
        return ExecutionPlan(
            plan_id=f"plan_{uuid4().hex}",
            **plan_payload,
            plan_hash=_fingerprint(plan_payload),
        )


def _fingerprint(value: dict[str, Any]) -> str:
    """计算可复现 SHA-256 摘要，审计只保存输入特征而不重复保存用户原文。"""
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _execution_mode(executor_profile: str) -> ExecutionMode:
    """将发布 Profile 映射为可解释运行模式，避免 Planner 依赖具体执行器实例。"""
    return ExecutionMode(legacy_execution_mode(executor_profile))


def _complexity(
    state: dict[str, Any],
    intent: IntentResult,
    entities: list[EntityResult],
    sources: SourcePlan,
) -> ComplexityAssessment:
    """按来源、写操作、审批和实体数等可解释信号评分，不依赖模型自评。"""
    score = 5
    reasons: list[str] = []

    def add(points: int, reason: str) -> None:
        """累计受上限约束的评分，并记录每一项业务原因。"""
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
    """由剩余绝对时间和复杂度得出 SLA 可行性，不承诺无法完成的质量等级。"""
    minimum = (
        1_000 if complexity.level == "simple" else 3_000 if complexity.level == "medium" else 8_000
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
    """给路由使用的保守估算；实际成本仍以 Gateway 账单为准。"""
    base = 0.01 if complexity.level == "simple" else 0.03 if complexity.level == "medium" else 0.08
    return round(base + 0.002 * (len(sources.knowledge_bases) + len(sources.context_sources)), 6)


def _route(
    intent: IntentResult,
    sources: SourcePlan,
    complexity: ComplexityAssessment,
    sla: SlaAssessment,
    cost: CostAssessment,
) -> RouteDecision:
    """根据置信度、SLA、成本和副作用选择受控路径及允许降级链。"""
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
    """生成下游网关所需运行关联与剩余预算头，不传播任意客户端 Header。"""
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
                float(budget.get("max_cost_usd", 0)) - float(budget.get("spent_cost_usd", 0)),
            )
        ),
        "X-Data-Region": str(state.get("compiled_plan", {}).get("data_region") or "unspecified"),
    }


def select_logical_model(
    snapshot: dict[str, Any],
    fallback: str,
    compiled_plan: dict[str, Any] | None = None,
) -> str:
    """优先从已编译计划选择逻辑模型，缺失时才使用发布策略和本地回退。"""
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

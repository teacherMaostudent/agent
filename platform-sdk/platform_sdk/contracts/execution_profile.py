"""执行生命周期与部署 Profile 的唯一共享解析规则。

发布、能力校验和 Runtime 必须使用同一映射。旧 ``runtime_executor`` 仅作为
迁移输入；一旦给出 ``execution`` 双轴声明，它就是唯一权威来源。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ExecutionProfileError(ValueError):
    """执行声明无法解析为平台已知 Profile 时抛出。"""


class PlanningStrategy(StrEnum):
    """宏观任务控制策略；它不等同于单步模型推理方式。"""

    PLAN_EXECUTE = "plan_execute"


class StepExecutionStrategy(StrEnum):
    """一个已准入步骤内部允许采用的执行方式。"""

    DETERMINISTIC = "deterministic"
    REACT = "react"


class ExecutionEngine(StrEnum):
    """实际解释任务步骤的执行内核。"""

    SIMPLE = "simple"
    LANGGRAPH = "langgraph"
    DEEP_AGENTS = "deep_agents"


class DurabilityStrategy(StrEnum):
    """执行历史的持久恢复策略，与执行内核正交。"""

    EPHEMERAL = "ephemeral"
    CHECKPOINTED = "checkpointed"
    TEMPORAL = "temporal"


class ContextStrategy(StrEnum):
    """事实、证据、产物与决策的上下文管理策略。"""

    MANAGED_LEDGER = "managed_ledger"


class ToolPresentationMode(StrEnum):
    """模型可见工具的受控呈现方式。"""

    NATIVE = "native"


class ExecutionRequirements(BaseModel):
    """跨服务可序列化的正交执行维度声明。

    ``lifecycle``/``reasoning`` 是旧快照兼容投影；新发布应同时冻结明确的
    engine、durability、planning、step、context 与 tool presentation。
    """

    lifecycle: str = Field(
        default="request_scoped", pattern="^(request_scoped|durable_workflow)$"
    )
    reasoning: str = Field(default="graph", pattern="^(minimal|agentic|graph)$")
    engine: ExecutionEngine | None = None
    durability: DurabilityStrategy | None = None
    planning_strategy: PlanningStrategy = PlanningStrategy.PLAN_EXECUTE
    default_step_strategy: StepExecutionStrategy = StepExecutionStrategy.DETERMINISTIC
    adaptive_step_strategy: StepExecutionStrategy = StepExecutionStrategy.REACT
    context_strategy: ContextStrategy = ContextStrategy.MANAGED_LEDGER
    tool_presentation: ToolPresentationMode = ToolPresentationMode.NATIVE

    @model_validator(mode="after")
    def validate_orthogonal_projection(self) -> ExecutionRequirements:
        """新维度是事实源; 显式旧字段与其冲突时拒绝，否则生成一致迁移投影。"""
        if self.engine is not None:
            expected_reasoning = {
                ExecutionEngine.SIMPLE: "minimal",
                ExecutionEngine.DEEP_AGENTS: "agentic",
                ExecutionEngine.LANGGRAPH: "graph",
            }[self.engine]
            if (
                "reasoning" in self.model_fields_set
                and self.reasoning != expected_reasoning
            ):
                raise ValueError("reasoning conflicts with execution engine")
            self.reasoning = type(self.reasoning)(expected_reasoning)
        if self.durability is not None:
            expected_lifecycle = (
                "durable_workflow"
                if self.durability == DurabilityStrategy.TEMPORAL
                else "request_scoped"
            )
            if (
                "lifecycle" in self.model_fields_set
                and self.lifecycle != expected_lifecycle
            ):
                raise ValueError("lifecycle conflicts with durability strategy")
            self.lifecycle = type(self.lifecycle)(expected_lifecycle)
        return self

    def normalized_engine(self) -> ExecutionEngine:
        """将旧 reasoning 投影为执行引擎，供迁移期目录统一解析。"""
        if self.engine is not None:
            return self.engine
        return {
            "minimal": ExecutionEngine.SIMPLE,
            # 旧 agentic/v1 实际由受控 LangGraph Loop 执行，不能冒充 Deep Agents。
            "agentic": ExecutionEngine.LANGGRAPH,
            "graph": ExecutionEngine.LANGGRAPH,
        }[self.reasoning]

    def normalized_durability(self) -> DurabilityStrategy:
        """将旧 lifecycle 投影为耐久策略，避免把 Temporal 当成执行器。"""
        if self.durability is not None:
            return self.durability
        return (
            DurabilityStrategy.TEMPORAL
            if self.lifecycle == "durable_workflow"
            else DurabilityStrategy.EPHEMERAL
        )


PROFILE_BY_REQUIREMENTS: dict[tuple[str, str], str] = {
    ("request_scoped", "minimal"): "simple/v1",
    ("request_scoped", "agentic"): "agentic/v1",
    ("request_scoped", "graph"): "declarative-langgraph/v1",
    ("durable_workflow", "minimal"): "temporal-simple/v1",
    ("durable_workflow", "agentic"): "temporal-agentic/v1",
    ("durable_workflow", "graph"): "temporal-workflow/v1",
}
PROFILE_BY_EXECUTION: dict[tuple[ExecutionEngine, DurabilityStrategy], str] = {
    (ExecutionEngine.SIMPLE, DurabilityStrategy.EPHEMERAL): "simple/v1",
    (
        ExecutionEngine.LANGGRAPH,
        DurabilityStrategy.EPHEMERAL,
    ): "declarative-langgraph/v1",
    (ExecutionEngine.DEEP_AGENTS, DurabilityStrategy.EPHEMERAL): "deep-agents/v1",
    (ExecutionEngine.SIMPLE, DurabilityStrategy.CHECKPOINTED): "checkpointed-simple/v1",
    (
        ExecutionEngine.LANGGRAPH,
        DurabilityStrategy.CHECKPOINTED,
    ): "checkpointed-langgraph/v1",
    (
        ExecutionEngine.DEEP_AGENTS,
        DurabilityStrategy.CHECKPOINTED,
    ): "checkpointed-deep-agents/v1",
    (ExecutionEngine.SIMPLE, DurabilityStrategy.TEMPORAL): "temporal-simple/v1",
    (ExecutionEngine.LANGGRAPH, DurabilityStrategy.TEMPORAL): "temporal-workflow/v1",
    (
        ExecutionEngine.DEEP_AGENTS,
        DurabilityStrategy.TEMPORAL,
    ): "temporal-deep-agents/v1",
}
REQUIREMENTS_BY_PROFILE: dict[str, ExecutionRequirements] = {
    profile: ExecutionRequirements(lifecycle=lifecycle, reasoning=reasoning)
    for (lifecycle, reasoning), profile in PROFILE_BY_REQUIREMENTS.items()
}
REQUIREMENTS_BY_PROFILE.update(
    {
        profile: ExecutionRequirements(
            lifecycle=(
                "durable_workflow"
                if durability == DurabilityStrategy.TEMPORAL
                else "request_scoped"
            ),
            reasoning={
                ExecutionEngine.SIMPLE: "minimal",
                ExecutionEngine.DEEP_AGENTS: "agentic",
                ExecutionEngine.LANGGRAPH: "graph",
            }[engine],
            engine=engine,
            durability=durability,
        )
        for (engine, durability), profile in PROFILE_BY_EXECUTION.items()
    }
)


def resolve_execution_profile(
    spec: Mapping[str, Any],
) -> tuple[ExecutionRequirements, str]:
    """解析双轴声明或旧 Profile，并拒绝二者表达不同执行事实的快照。"""
    raw = spec.get("execution")
    legacy_profile = str(spec.get("runtime_executor") or "").strip()
    if raw is not None:
        if not isinstance(raw, Mapping):
            raise ExecutionProfileError(
                "published execution requirements must be an object"
            )
        try:
            requirements = ExecutionRequirements.model_validate(raw)
        except ValueError as exc:
            raise ExecutionProfileError(
                f"published execution requirements are invalid: {exc}"
            ) from exc
        if raw.get("engine") is None and raw.get("durability") is None:
            profile = PROFILE_BY_REQUIREMENTS[
                (requirements.lifecycle, requirements.reasoning)
            ]
        else:
            profile = PROFILE_BY_EXECUTION[
                (requirements.normalized_engine(), requirements.normalized_durability())
            ]
        # Default values written by old clients are not an explicit conflicting choice.
        if (
            legacy_profile
            and legacy_profile != "declarative-langgraph/v1"
            and legacy_profile != profile
        ):
            raise ExecutionProfileError(
                "runtime_executor conflicts with execution requirements; "
                f"expected {profile}, got {legacy_profile}"
            )
        return requirements, profile
    # Historical v1 snapshots predate an explicit execution field.  Their
    # compiler contract defined the graph profile as the migration default;
    # retain that one deterministic interpretation instead of breaking old
    # immutable releases at read time.
    if not legacy_profile:
        legacy_profile = "declarative-langgraph/v1"
    try:
        return REQUIREMENTS_BY_PROFILE[legacy_profile], legacy_profile
    except KeyError as exc:
        raise ExecutionProfileError(
            f"published runtime executor profile is unknown: {legacy_profile}"
        ) from exc


def legacy_execution_mode(profile: str) -> str:
    """为旧 API/审计投影提供稳定模式；耐久性优先于推理形态。"""
    requirements = REQUIREMENTS_BY_PROFILE.get(profile)
    if requirements is None:
        return "graph"
    if requirements.lifecycle == "durable_workflow":
        return "durable"
    if requirements.reasoning == "minimal":
        return "fast"
    if requirements.reasoning == "agentic":
        return "agentic"
    return "graph"

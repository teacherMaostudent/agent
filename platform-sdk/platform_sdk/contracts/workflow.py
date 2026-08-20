"""发布 Graph 条件的共享、无副作用 DSL 契约。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from platform_sdk.contracts.skills import (
    CapabilityProviderDescriptor,
    CapabilityRoutingPolicy,
    OrchestrationOwner,
)

_CONDITION = re.compile(
    r"^\s*([a-z]+(?:\.[a-z_]+)?)\s*(==|!=|>=|<=|>|<)\s*"
    r'("(?:[^"\\]|\\.)*"|true|false|-?\d+)\s*$',
    re.IGNORECASE,
)
_FIELDS = {
    "decision.action",
    "intent.name",
    "intent.confidence",
    "evidence.count",
    "tool.success",
    "budget.remaining_cost_usd",
    "budget.remaining_ms",
}
_OPERATORS = {"==", "!=", ">", ">=", "<", "<="}


class WorkflowConditionError(ValueError):
    """表示发布条件不能被受限 DSL 安全、确定性地解释。"""


class WorkflowCondition(BaseModel):
    """已编译的 Graph 边条件；只引用 Runtime 提供的白名单事实。"""

    field: str
    operator: Literal["==", "!=", ">", ">=", "<", "<="]
    value: str | bool | int | float


class WorkflowStep(BaseModel):
    """零 Agent Workflow 的受控步骤；仅声明能力，不允许嵌入回调或代码地址。"""

    step_id: str = Field(min_length=1, max_length=100)
    capability_id: str = Field(min_length=2, max_length=160)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    max_attempts: int = Field(default=3, ge=1, le=20)
    compensation_capability_id: str = Field(default="", max_length=160)


class WorkflowSpec(BaseModel):
    """独立可发布 Workflow 声明；固定顺序由 Workflow 拥有而非 Agent 决定。"""

    workflow_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    description: str = Field(default="", max_length=2_000)
    durable: bool = True
    steps: list[WorkflowStep] = Field(min_length=1, max_length=500)
    capability_providers: list[CapabilityProviderDescriptor] = Field(
        default_factory=list, max_length=500
    )
    capability_routing: list[CapabilityRoutingPolicy] = Field(
        default_factory=list, max_length=500
    )
    owner: Literal[OrchestrationOwner.WORKFLOW] = OrchestrationOwner.WORKFLOW


class CompiledWorkflowPlan(BaseModel):
    """发布期冻结的零 Agent 工作流工件，Runtime 不重新解释草稿。"""

    contract_version: str = "workflow-plan/v1"
    workflow_id: str
    version: str
    durable: bool = True
    steps: list[WorkflowStep]
    capability_providers: list[CapabilityProviderDescriptor] = Field(
        default_factory=list
    )
    capability_routing: list[CapabilityRoutingPolicy] = Field(default_factory=list)
    owner: Literal[OrchestrationOwner.WORKFLOW] = OrchestrationOwner.WORKFLOW


def compile_workflow_plan(
    spec: WorkflowSpec, version: str
) -> tuple[CompiledWorkflowPlan, str]:
    """把可变 Workflow Draft 冻结为顺序执行计划及稳定摘要。"""
    if not version.strip():
        raise ValueError("workflow version must not be empty")
    step_ids = [item.step_id for item in spec.steps]
    if len(step_ids) != len(set(step_ids)):
        raise ValueError("workflow step_id values must be unique")
    _validate_workflow_capabilities(spec)
    payload = {
        "contract_version": "workflow-plan/v1",
        "workflow_id": spec.workflow_id,
        "version": version,
        "durable": spec.durable,
        "steps": [item.model_dump(mode="json") for item in spec.steps],
        "capability_providers": [
            item.model_dump(mode="json") for item in spec.capability_providers
        ],
        "capability_routing": [
            item.model_dump(mode="json") for item in spec.capability_routing
        ],
        "owner": OrchestrationOwner.WORKFLOW.value,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return CompiledWorkflowPlan.model_validate(payload), digest


def _validate_workflow_capabilities(spec: WorkflowSpec) -> None:
    """确保每个步骤都有发布期冻结的 Provider 及唯一路由策略。"""
    provider_ids = [item.provider_id for item in spec.capability_providers]
    if len(provider_ids) != len(set(provider_ids)):
        raise ValueError("workflow capability provider IDs must be unique")
    policies = {item.capability_id: item for item in spec.capability_routing}
    if len(policies) != len(spec.capability_routing):
        raise ValueError("workflow capability routing policies must be unique")
    provided = {
        capability.capability_id
        for provider in spec.capability_providers
        for capability in provider.capabilities
    }
    for step in spec.steps:
        capability = step.capability_id.strip().upper()
        if spec.capability_providers and capability not in provided:
            raise ValueError(f"workflow step has no provider: {capability}")
        policy = policies.get(capability)
        if policy and not set(policy.provider_order) <= set(provider_ids):
            raise ValueError(
                f"workflow routing references unknown provider: {capability}"
            )


def compile_workflow_condition(expression: str | None) -> WorkflowCondition | None:
    """把简短声明式条件解析成结构化规则，拒绝代码、函数和未知字段。"""
    if expression is None or not expression.strip():
        return None
    match = _CONDITION.fullmatch(expression)
    if match is None:
        raise WorkflowConditionError(
            "condition must use '<field> <operator> <literal>' DSL"
        )
    field, operator, raw_value = match.groups()
    field = field.lower()
    if field not in _FIELDS:
        raise WorkflowConditionError(f"condition field is not allowed: {field}")
    if operator not in _OPERATORS:
        raise WorkflowConditionError(f"condition operator is not allowed: {operator}")
    value = _literal(raw_value)
    if isinstance(value, (int, float)) and field in {"decision.action", "intent.name"}:
        raise WorkflowConditionError(
            f"condition field requires string literal: {field}"
        )
    if isinstance(value, bool) and field not in {"tool.success"}:
        raise WorkflowConditionError(
            f"condition field does not accept boolean: {field}"
        )
    if field == "tool.success" and not isinstance(value, bool):
        raise WorkflowConditionError("tool.success requires true or false")
    return WorkflowCondition(field=field, operator=operator, value=value)


def evaluate_workflow_condition(
    condition: WorkflowCondition | dict[str, Any] | None,
    facts: dict[str, Any],
) -> bool:
    """以显式事实判断已编译条件；类型不匹配和缺失事实默认不命中。"""
    if condition is None:
        return True
    parsed = WorkflowCondition.model_validate(condition)
    left = facts.get(parsed.field)
    if left is None or type(left) is not type(parsed.value):
        return False
    return {
        "==": left == parsed.value,
        "!=": left != parsed.value,
        ">": left > parsed.value,
        ">=": left >= parsed.value,
        "<": left < parsed.value,
        "<=": left <= parsed.value,
    }[parsed.operator]


def _literal(raw_value: str) -> str | bool | int | float:
    """解析不含表达能力的字面量，避免 eval、模板渲染和隐式类型转换。"""
    lowered = raw_value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if raw_value.startswith('"'):
        return bytes(raw_value[1:-1], "utf-8").decode("unicode_escape")
    return float(raw_value) if "." in raw_value else int(raw_value)

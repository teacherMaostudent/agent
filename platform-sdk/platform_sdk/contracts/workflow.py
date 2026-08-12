"""发布 Graph 条件的共享、无副作用 DSL 契约。"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel

_CONDITION = re.compile(
    r'^\s*([a-z]+(?:\.[a-z_]+)?)\s*(==|!=|>=|<=|>|<)\s*'
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


def compile_workflow_condition(expression: str | None) -> WorkflowCondition | None:
    """把简短声明式条件解析成结构化规则，拒绝代码、函数和未知字段。"""
    if expression is None or not expression.strip():
        return None
    match = _CONDITION.fullmatch(expression)
    if match is None:
        raise WorkflowConditionError("condition must use '<field> <operator> <literal>' DSL")
    field, operator, raw_value = match.groups()
    field = field.lower()
    if field not in _FIELDS:
        raise WorkflowConditionError(f"condition field is not allowed: {field}")
    if operator not in _OPERATORS:
        raise WorkflowConditionError(f"condition operator is not allowed: {operator}")
    value = _literal(raw_value)
    if isinstance(value, (int, float)) and field in {"decision.action", "intent.name"}:
        raise WorkflowConditionError(f"condition field requires string literal: {field}")
    if isinstance(value, bool) and field not in {"tool.success"}:
        raise WorkflowConditionError(f"condition field does not accept boolean: {field}")
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

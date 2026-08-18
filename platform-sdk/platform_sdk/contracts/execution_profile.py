"""执行生命周期与部署 Profile 的唯一共享解析规则。

发布、能力校验和 Runtime 必须使用同一映射。旧 ``runtime_executor`` 仅作为
迁移输入；一旦给出 ``execution`` 双轴声明，它就是唯一权威来源。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field


class ExecutionProfileError(ValueError):
    """执行声明无法解析为平台已知 Profile 时抛出。"""


class ExecutionRequirements(BaseModel):
    """跨服务可序列化的执行耐久性与推理形态声明。"""

    lifecycle: str = Field(default="request_scoped", pattern="^(request_scoped|durable_workflow)$")
    reasoning: str = Field(default="graph", pattern="^(minimal|agentic|graph)$")


PROFILE_BY_REQUIREMENTS: dict[tuple[str, str], str] = {
    ("request_scoped", "minimal"): "simple/v1",
    ("request_scoped", "agentic"): "agentic/v1",
    ("request_scoped", "graph"): "declarative-langgraph/v1",
    ("durable_workflow", "minimal"): "temporal-simple/v1",
    ("durable_workflow", "agentic"): "temporal-agentic/v1",
    ("durable_workflow", "graph"): "temporal-workflow/v1",
}
REQUIREMENTS_BY_PROFILE: dict[str, ExecutionRequirements] = {
    profile: ExecutionRequirements(lifecycle=lifecycle, reasoning=reasoning)
    for (lifecycle, reasoning), profile in PROFILE_BY_REQUIREMENTS.items()
}


def resolve_execution_profile(spec: Mapping[str, Any]) -> tuple[ExecutionRequirements, str]:
    """解析双轴声明或旧 Profile，并拒绝二者表达不同执行事实的快照。"""
    raw = spec.get("execution")
    legacy_profile = str(spec.get("runtime_executor") or "").strip()
    if raw is not None:
        if not isinstance(raw, Mapping):
            raise ExecutionProfileError("published execution requirements must be an object")
        try:
            requirements = ExecutionRequirements.model_validate(raw)
        except ValueError as exc:
            raise ExecutionProfileError(f"published execution requirements are invalid: {exc}") from exc
        profile = PROFILE_BY_REQUIREMENTS[(requirements.lifecycle, requirements.reasoning)]
        # Default values written by old clients are not an explicit conflicting choice.
        if legacy_profile and legacy_profile != "declarative-langgraph/v1" and legacy_profile != profile:
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
        raise ExecutionProfileError(f"published runtime executor profile is unknown: {legacy_profile}") from exc


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

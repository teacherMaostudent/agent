"""发布快照所需 Runtime 能力的共享、确定性声明。"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class RuntimeCapability(StrEnum):
    """Runtime 可提供给已发布执行计划的固定外围能力。"""

    CONTEXT = "context"
    LLM = "llm"
    RETRIEVAL = "retrieval"
    TOOL = "tool"
    WORKFLOW = "workflow"


CAPABILITY_CONTRACT_VERSION = "runtime-capability-contract/v1"


def required_runtime_capabilities(spec: Mapping[str, Any]) -> list[str]:
    """从 Agent Spec 推导运行所需能力，避免快照手工填写后发生漂移。

    推导规则故意保守且可审计：普通声明式图需要 Context 与 LLM，知识和工具绑定
    分别要求检索和工具能力，Temporal Profile 还必须有耐久 Workflow 调度能力。
    ``simple/v1`` 是唯一明确的无外围依赖短任务执行器。
    """
    profile = str(spec.get("runtime_executor") or "declarative-langgraph/v1").strip()
    if profile == "simple/v1":
        return []

    required = {RuntimeCapability.CONTEXT.value, RuntimeCapability.LLM.value}
    if spec.get("knowledge"):
        required.add(RuntimeCapability.RETRIEVAL.value)
    if spec.get("tools"):
        required.add(RuntimeCapability.TOOL.value)
    if profile == "temporal-workflow/v1":
        required.add(RuntimeCapability.WORKFLOW.value)
    return sorted(required)

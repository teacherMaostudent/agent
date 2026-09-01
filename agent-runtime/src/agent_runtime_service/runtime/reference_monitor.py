"""Runtime 侧不可绕过的工具提议参考监控器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_runtime_service.runtime.models import RuntimeLimitExceeded
from agent_runtime_service.runtime.tool_execution import (
    SideEffectBarrier,
    SideEffectBarrierOutcome,
    SideEffectBarrierRejected,
    ToolExecutionPolicy,
)


@dataclass(frozen=True)
class ToolProposalAdmission:
    """Reference Monitor 的无副作用结论，供 Graph 路由与审计使用。"""

    binding: dict[str, Any]
    outcome: SideEffectBarrierOutcome


class RuntimeReferenceMonitor:
    """在 Runtime → Gateway 边界前执行完整性、范围与副作用前检查。

    它不取代 Gateway 的最终授权。其职责是拒绝未发布工具、越过准入计划范围的提议，
    并在不可逆调用前检查 Steering/取消与快照绑定。
    """

    def __init__(self, side_effect_barrier: SideEffectBarrier) -> None:
        self._side_effect_barrier = side_effect_barrier

    def evaluate(
        self,
        state: dict[str, Any],
        *,
        tool_name: str,
        binding: dict[str, Any],
        policy: ToolExecutionPolicy,
        tool_execution_id: str,
    ) -> ToolProposalAdmission:
        """对一次模型工具提议做 fail-closed 预授权，不执行企业操作。"""
        if state.get("compiled_plan", {}).get("contract_hash"):
            allowed = set(state.get("plan_admission", {}).get("allowed_tool_scope", []))
            if tool_name not in allowed:
                raise RuntimeLimitExceeded(
                    "TOOL_OUTSIDE_ADMITTED_SCOPE",
                    f"Tool '{tool_name}' is outside the admitted plan scope.",
                )
        if not binding:
            raise RuntimeLimitExceeded("TOOL_NOT_PUBLISHED", "Tool binding is missing.")
        try:
            outcome = self._side_effect_barrier.before_dispatch(
                state, policy, tool_execution_id=tool_execution_id
            )
        except SideEffectBarrierRejected as exc:
            raise RuntimeLimitExceeded("SIDE_EFFECT_BARRIER_REJECTED", str(exc)) from exc
        return ToolProposalAdmission(binding=binding, outcome=outcome)

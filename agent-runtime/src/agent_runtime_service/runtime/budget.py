"""Hard Runtime resource limits independent of model cooperation.

Reservations happen before LLM/tool calls.  This keeps a stochastic planner
from exceeding the published budget even if a downstream provider later fails.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent_runtime_service.runtime.models import RuntimeBudget, RuntimeLimitExceeded


class BudgetGuard:
    """Deterministic hard limits enforced outside model decisions."""

    def __init__(self, llm_call_reservation_usd: float, tool_call_reservation_usd: float) -> None:
        """保存单次调用的保守预留成本。

        预留发生在下游请求之前；即使供应商失败或不返回计费，也不会让随机规划绕过
        已发布预算。真实账单随后由 ``reconcile_cost`` 覆盖预留值。
        """
        self.llm_call_reservation_usd = llm_call_reservation_usd
        self.tool_call_reservation_usd = tool_call_reservation_usd

    def ensure_active(self, budget: RuntimeBudget) -> None:
        """检查绝对截止时间；超时必须在发起下一次外部调用前失败关闭。"""
        if datetime.now(UTC) >= budget.deadline_at:
            raise RuntimeLimitExceeded("DEADLINE_EXCEEDED", "The run deadline has expired.")

    def reserve_llm(self, budget: RuntimeBudget) -> RuntimeBudget:
        """为一次 LLM 调用原子递增调用数、尝试数与成本预留。"""
        self.ensure_active(budget)
        if budget.llm_calls >= budget.max_llm_calls:
            raise RuntimeLimitExceeded("MAX_LLM_CALLS", "The LLM call budget is exhausted.")
        return self._reserve(
            budget,
            llm_calls=budget.llm_calls + 1,
            cost=self.llm_call_reservation_usd,
        )

    def reserve_tool(self, budget: RuntimeBudget) -> RuntimeBudget:
        """为一次工具调用预留费用；审批并不免除工具预算。"""
        self.ensure_active(budget)
        if budget.tool_calls >= budget.max_tool_calls:
            raise RuntimeLimitExceeded("MAX_TOOL_CALLS", "The tool call budget is exhausted.")
        return self._reserve(
            budget,
            tool_calls=budget.tool_calls + 1,
            cost=self.tool_call_reservation_usd,
        )

    def reserve_retrieval(self, budget: RuntimeBudget) -> RuntimeBudget:
        """消耗一轮检索及一次下游尝试，不按零费用无限循环检索。"""
        self.ensure_active(budget)
        if budget.retrieval_rounds >= budget.max_retrieval_rounds:
            raise RuntimeLimitExceeded(
                "MAX_RETRIEVAL_ROUNDS",
                "The retrieval-round budget is exhausted.",
            )
        self._ensure_attempt(budget)
        return budget.model_copy(
            update={
                "retrieval_rounds": budget.retrieval_rounds + 1,
                "attempts_used": budget.attempts_used + 1,
            }
        )

    def count_step(self, budget: RuntimeBudget) -> RuntimeBudget:
        """在决策前消耗一个 Agent 步，防止模型反复选择无副作用动作。"""
        self.ensure_active(budget)
        if budget.step_count >= budget.max_steps:
            raise RuntimeLimitExceeded("MAX_STEPS", "The agent step budget is exhausted.")
        return budget.model_copy(update={"step_count": budget.step_count + 1})

    def reconcile_cost(
        self,
        budget: RuntimeBudget,
        *,
        reserved_usd: float,
        actual_usd: float | None,
    ) -> RuntimeBudget:
        """以网关报告的实际成本替换预留；实际超支也必须拒绝继续执行。"""
        if actual_usd is None:
            return budget
        next_cost = max(0.0, budget.spent_cost_usd - reserved_usd + actual_usd)
        if next_cost > budget.max_cost_usd:
            raise RuntimeLimitExceeded(
                "COST_BUDGET_EXCEEDED",
                "Actual gateway usage exceeded the run cost budget.",
            )
        return budget.model_copy(update={"spent_cost_usd": next_cost})

    @staticmethod
    def _reserve(
        budget: RuntimeBudget,
        *,
        llm_calls: int | None = None,
        tool_calls: int | None = None,
        cost: float,
    ) -> RuntimeBudget:
        """统一执行成本与尝试预留，返回新模型而不原地修改检查点状态。"""
        next_cost = budget.spent_cost_usd + cost
        if next_cost > budget.max_cost_usd:
            raise RuntimeLimitExceeded("COST_BUDGET_EXCEEDED", "The run cost budget is exhausted.")
        BudgetGuard._ensure_attempt(budget)
        updates = {
            "spent_cost_usd": next_cost,
            "attempts_used": budget.attempts_used + 1,
        }
        if llm_calls is not None:
            updates["llm_calls"] = llm_calls
        if tool_calls is not None:
            updates["tool_calls"] = tool_calls
        return budget.model_copy(update=updates)

    @staticmethod
    def _ensure_attempt(budget: RuntimeBudget) -> None:
        """确保每次跨服务尝试都未耗尽发布的总尝试额度。"""
        if budget.attempts_used >= budget.max_attempts:
            raise RuntimeLimitExceeded(
                "ATTEMPT_BUDGET_EXCEEDED",
                "The downstream attempt budget is exhausted.",
            )

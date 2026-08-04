"""Hard Runtime resource limits independent of model cooperation.

Reservations happen before LLM/tool calls.  This keeps a stochastic planner
from exceeding the published budget even if a downstream provider later fails.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.runtime.models import RuntimeBudget, RuntimeLimitExceeded


class BudgetGuard:
    """Deterministic hard limits enforced outside model decisions."""

    def __init__(
        self, llm_call_reservation_usd: float, tool_call_reservation_usd: float
    ) -> None:
        self.llm_call_reservation_usd = llm_call_reservation_usd
        self.tool_call_reservation_usd = tool_call_reservation_usd

    def ensure_active(self, budget: RuntimeBudget) -> None:
        if datetime.now(UTC) >= budget.deadline_at:
            raise RuntimeLimitExceeded(
                "DEADLINE_EXCEEDED", "The run deadline has expired."
            )

    def reserve_llm(self, budget: RuntimeBudget) -> RuntimeBudget:
        self.ensure_active(budget)
        if budget.llm_calls >= budget.max_llm_calls:
            raise RuntimeLimitExceeded(
                "MAX_LLM_CALLS", "The LLM call budget is exhausted."
            )
        return self._reserve(
            budget,
            llm_calls=budget.llm_calls + 1,
            cost=self.llm_call_reservation_usd,
        )

    def reserve_tool(self, budget: RuntimeBudget) -> RuntimeBudget:
        self.ensure_active(budget)
        if budget.tool_calls >= budget.max_tool_calls:
            raise RuntimeLimitExceeded(
                "MAX_TOOL_CALLS", "The tool call budget is exhausted."
            )
        return self._reserve(
            budget,
            tool_calls=budget.tool_calls + 1,
            cost=self.tool_call_reservation_usd,
        )

    def reserve_retrieval(self, budget: RuntimeBudget) -> RuntimeBudget:
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
        self.ensure_active(budget)
        if budget.step_count >= budget.max_steps:
            raise RuntimeLimitExceeded(
                "MAX_STEPS", "The agent step budget is exhausted."
            )
        return budget.model_copy(update={"step_count": budget.step_count + 1})

    def reconcile_cost(
        self,
        budget: RuntimeBudget,
        *,
        reserved_usd: float,
        actual_usd: float | None,
    ) -> RuntimeBudget:
        """Replace an estimate with reported cost and fail closed on overrun."""
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
        next_cost = budget.spent_cost_usd + cost
        if next_cost > budget.max_cost_usd:
            raise RuntimeLimitExceeded(
                "COST_BUDGET_EXCEEDED", "The run cost budget is exhausted."
            )
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
        if budget.attempts_used >= budget.max_attempts:
            raise RuntimeLimitExceeded(
                "ATTEMPT_BUDGET_EXCEEDED",
                "The downstream attempt budget is exhausted.",
            )

"""执行停止策略的组合边界。

模型可以提出下一步，但不能决定是否忽略取消、截止时间或资源额度。停止策略聚合执行
平面硬限制，并向 Graph/Executor 返回稳定的机器可读结论，而不是让各节点散落判断。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from agent_runtime_service.runtime.budget import BudgetGuard
from agent_runtime_service.runtime.models import (
    RuntimeBudget,
    RuntimeCancelled,
    RuntimeLimitExceeded,
)


class StopReason(StrEnum):
    """停止决策的规范原因，供状态机、API 与治理事件共享。"""

    CONTINUE = "CONTINUE"
    CANCELLED = "CANCELLED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"


@dataclass(frozen=True)
class StopDecision:
    """一次副作用前检查的确定性结果。"""

    reason: StopReason
    message: str = ""

    @property
    def should_stop(self) -> bool:
        """表明调用方是否必须停止继续请求模型、工具或检索服务。"""
        return self.reason != StopReason.CONTINUE


class StopPolicy(Protocol):
    """可组合停止规则的窄接口，不暴露 Graph 或业务领域对象。"""

    def evaluate(self, state: dict[str, Any]) -> StopDecision:
        """检查当前执行状态，并且不修改检查点中的任何业务字段。"""
        ...


class BudgetStopPolicy:
    """复用预算守卫的绝对截止时间检查，避免维护两套时间判断。"""

    def __init__(self, guard: BudgetGuard) -> None:
        """注入 Graph 已使用的预算守卫，成本/调用额度仍由具体动作预留。"""
        self._guard = guard

    def evaluate(self, state: dict[str, Any]) -> StopDecision:
        """在下一副作用前检查截止时间，并转换为统一停止原因。"""
        # 图的 ``load_memory`` 节点先于 Planner 创建预算；此时仅取消策略可判断，
        # 不能因尚未生成预算把兼容的初始化路径误判为失败。
        if "budget" not in state:
            return StopDecision(StopReason.CONTINUE)
        try:
            self._guard.ensure_active(RuntimeBudget.model_validate(state["budget"]))
        except RuntimeLimitExceeded as exc:
            if exc.code == "DEADLINE_EXCEEDED":
                return StopDecision(StopReason.DEADLINE_EXCEEDED, str(exc))
            raise
        return StopDecision(StopReason.CONTINUE)


class CancellationStopPolicy:
    """将持久化协作取消标记纳入统一停止策略。"""

    def __init__(self, checker: Callable[[str, str], bool] | None) -> None:
        """保存可选取消查询器；无查询器的本地测试路径默认可继续。"""
        self._checker = checker

    def evaluate(self, state: dict[str, Any]) -> StopDecision:
        """按租户和 Run ID 查询取消标记，不能依据调用方自报的状态判断。"""
        if self._checker and self._checker(str(state.get("tenant_id", "")), str(state.get("run_id", ""))):
            return StopDecision(StopReason.CANCELLED, "Run cancellation was requested.")
        return StopDecision(StopReason.CONTINUE)


class CompositeStopPolicy:
    """按固定优先级执行停止规则；首个阻断结论必须立即生效。"""

    def __init__(self, policies: Iterable[StopPolicy]) -> None:
        """冻结策略顺序，避免请求期插入规则而改变已发布执行语义。"""
        self._policies = tuple(policies)

    def evaluate(self, state: dict[str, Any]) -> StopDecision:
        """依次评估策略，后续规则不得覆盖已出现的安全停止结论。"""
        for policy in self._policies:
            decision = policy.evaluate(state)
            if decision.should_stop:
                return decision
        return StopDecision(StopReason.CONTINUE)

    def enforce(self, state: dict[str, Any]) -> None:
        """把停止结论映射为既有稳定异常，保持 API 与 LangGraph 返回兼容。"""
        decision = self.evaluate(state)
        if decision.reason == StopReason.CANCELLED:
            raise RuntimeCancelled(decision.message)
        if decision.reason == StopReason.DEADLINE_EXCEEDED:
            raise RuntimeLimitExceeded(decision.reason, decision.message)

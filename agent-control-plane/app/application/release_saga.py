"""Idempotent release Saga primitives.

The repository transaction remains the source of truth.  This coordinator
records completed steps so a worker retry cannot repeat an external side effect
and exposes a reconciliation result for an operator or scheduled worker.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class SagaState:
    saga_id: str
    completed: set[str] = field(default_factory=set)
    compensations: list[str] = field(default_factory=list)


class ReleaseSaga:
    def __init__(self, saga_id: str, state: SagaState | None = None) -> None:
        """恢复既有 Saga 状态或为一次发布创建新的幂等步骤记录。"""
        self.state = state or SagaState(saga_id)

    def step(
        self, name: str, action: Callable[[], None], compensate: Callable[[], None] | None = None
    ) -> None:
        """只执行未完成步骤；失败时运行局部补偿并保留记录以便对账重试。"""
        if name in self.state.completed:
            return
        try:
            action()
            self.state.completed.add(name)
        except Exception:
            if compensate is not None:
                compensate()
                self.state.compensations.append(name)
            raise


@dataclass(frozen=True)
class ReconciliationResult:
    desired_release_id: str
    observed_release_id: str | None
    consistent: bool
    action: str


def reconcile_release(
    desired_release_id: str, observed_release_id: str | None
) -> ReconciliationResult:
    """比较期望与外部观测发布，返回运维可执行的修复或暂停建议。"""
    consistent = desired_release_id == observed_release_id
    return ReconciliationResult(
        desired_release_id=desired_release_id,
        observed_release_id=observed_release_id,
        consistent=consistent,
        action="noop" if consistent else "repair_or_pause",
    )

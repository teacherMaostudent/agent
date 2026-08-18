"""Agent Run 的显式状态机及可审计迁移规则。

LangGraph 负责单个 Agent 图的节点推进，Temporal 负责长期工作流的耐久调度；本模块
只描述一次 Run 在执行平面上的生命周期。该规则不依赖模型输出，因此运行恢复、取消和
治理审计可以基于同一组确定性状态判断，而不必反推图内部实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AgentRunState(StrEnum):
    """一次 Agent Run 的规范执行状态，覆盖准备、外部副作用与终态。"""

    CREATED = "CREATED"
    PREPARING_CONTEXT = "PREPARING_CONTEXT"
    REQUESTING_MODEL = "REQUESTING_MODEL"
    EXECUTING_TOOLS = "EXECUTING_TOOLS"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RECONCILING = "RECONCILING"
    RETRY_WAIT = "RETRY_WAIT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        """返回该状态是否已终结，终态不能被新输入或重试重新激活。"""
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED}


class AgentRunEvent(StrEnum):
    """驱动状态转换的稳定事件名称；事件不等同于 LangGraph 的内部节点名称。"""

    START = "START"
    CONTEXT_READY = "CONTEXT_READY"
    MODEL_REQUESTED = "MODEL_REQUESTED"
    MODEL_COMPLETED = "MODEL_COMPLETED"
    MODEL_FAILED = "MODEL_FAILED"
    TOOL_INTENT_RECORDED = "TOOL_INTENT_RECORDED"
    TOOLS_COMPLETED = "TOOLS_COMPLETED"
    STEERING_RECEIVED = "STEERING_RECEIVED"
    FOLLOW_UP_RECEIVED = "FOLLOW_UP_RECEIVED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    INPUT_REQUIRED = "INPUT_REQUIRED"
    APPROVAL_RECEIVED = "APPROVAL_RECEIVED"
    RECONCILED = "RECONCILED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    RETRY_READY = "RETRY_READY"
    TIMEOUT = "TIMEOUT"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_FAILED = "RUN_FAILED"


class InvalidRunTransition(RuntimeError):
    """未声明的状态迁移被拒绝时抛出，调用方不得把它降级为继续执行。"""


@dataclass(frozen=True)
class RunTransition:
    """记录一次状态机判定，便于持久化层附加审计事件。"""

    previous: AgentRunState
    event: AgentRunEvent
    current: AgentRunState

    @property
    def changed(self) -> bool:
        """标记该事件是否真正改变状态，幂等重复不会污染事件流。"""
        return self.previous != self.current


_TRANSITIONS: dict[tuple[AgentRunState, AgentRunEvent], AgentRunState] = {
    (AgentRunState.CREATED, AgentRunEvent.START): AgentRunState.PREPARING_CONTEXT,
    (AgentRunState.CREATED, AgentRunEvent.RUN_COMPLETED): AgentRunState.COMPLETED,
    (AgentRunState.CREATED, AgentRunEvent.RUN_FAILED): AgentRunState.FAILED,
    (AgentRunState.CREATED, AgentRunEvent.CANCEL_REQUESTED): AgentRunState.CANCELLED,
    (AgentRunState.PREPARING_CONTEXT, AgentRunEvent.CONTEXT_READY): AgentRunState.REQUESTING_MODEL,
    (AgentRunState.PREPARING_CONTEXT, AgentRunEvent.APPROVAL_REQUIRED): AgentRunState.WAITING_APPROVAL,
    (AgentRunState.PREPARING_CONTEXT, AgentRunEvent.INPUT_REQUIRED): AgentRunState.WAITING_INPUT,
    (AgentRunState.REQUESTING_MODEL, AgentRunEvent.MODEL_REQUESTED): AgentRunState.REQUESTING_MODEL,
    (AgentRunState.REQUESTING_MODEL, AgentRunEvent.MODEL_COMPLETED): AgentRunState.RECONCILING,
    (AgentRunState.REQUESTING_MODEL, AgentRunEvent.TOOL_INTENT_RECORDED): AgentRunState.EXECUTING_TOOLS,
    (AgentRunState.REQUESTING_MODEL, AgentRunEvent.APPROVAL_REQUIRED): AgentRunState.WAITING_APPROVAL,
    (AgentRunState.REQUESTING_MODEL, AgentRunEvent.INPUT_REQUIRED): AgentRunState.WAITING_INPUT,
    (AgentRunState.REQUESTING_MODEL, AgentRunEvent.FOLLOW_UP_RECEIVED): AgentRunState.PREPARING_CONTEXT,
    (AgentRunState.REQUESTING_MODEL, AgentRunEvent.STEERING_RECEIVED): AgentRunState.PREPARING_CONTEXT,
    (AgentRunState.EXECUTING_TOOLS, AgentRunEvent.TOOLS_COMPLETED): AgentRunState.REQUESTING_MODEL,
    (AgentRunState.EXECUTING_TOOLS, AgentRunEvent.APPROVAL_REQUIRED): AgentRunState.WAITING_APPROVAL,
    (AgentRunState.EXECUTING_TOOLS, AgentRunEvent.RECOVERY_REQUIRED): AgentRunState.RECONCILING,
    (AgentRunState.WAITING_INPUT, AgentRunEvent.STEERING_RECEIVED): AgentRunState.PREPARING_CONTEXT,
    (AgentRunState.WAITING_INPUT, AgentRunEvent.FOLLOW_UP_RECEIVED): AgentRunState.PREPARING_CONTEXT,
    (AgentRunState.WAITING_APPROVAL, AgentRunEvent.APPROVAL_RECEIVED): AgentRunState.RECONCILING,
    (AgentRunState.RECONCILING, AgentRunEvent.RECONCILED): AgentRunState.REQUESTING_MODEL,
    # 审批恢复后通常会直接继续此前已计划的工具步骤，不要求虚构一次新的模型请求。
    (AgentRunState.RECONCILING, AgentRunEvent.TOOL_INTENT_RECORDED): AgentRunState.EXECUTING_TOOLS,
    (AgentRunState.RECONCILING, AgentRunEvent.STEERING_RECEIVED): AgentRunState.PREPARING_CONTEXT,
    (AgentRunState.RECONCILING, AgentRunEvent.FOLLOW_UP_RECEIVED): AgentRunState.PREPARING_CONTEXT,
    (AgentRunState.RECONCILING, AgentRunEvent.RETRY_SCHEDULED): AgentRunState.RETRY_WAIT,
    (AgentRunState.RETRY_WAIT, AgentRunEvent.RETRY_READY): AgentRunState.RECONCILING,
}

_GLOBAL_TRANSITIONS: dict[AgentRunEvent, AgentRunState] = {
    AgentRunEvent.CANCEL_REQUESTED: AgentRunState.CANCELLED,
    AgentRunEvent.TIMEOUT: AgentRunState.FAILED,
    AgentRunEvent.BUDGET_EXHAUSTED: AgentRunState.FAILED,
    AgentRunEvent.RUN_COMPLETED: AgentRunState.COMPLETED,
    AgentRunEvent.RUN_FAILED: AgentRunState.FAILED,
}


def transition_run_state(
    current: AgentRunState | str, event: AgentRunEvent | str
) -> RunTransition:
    """严格计算下一状态；只有相同事件的安全重放可作为幂等无操作通过。"""
    previous = AgentRunState(current)
    trigger = AgentRunEvent(event)
    if previous.terminal:
        target = _GLOBAL_TRANSITIONS.get(trigger)
        if target == previous:
            return RunTransition(previous, trigger, previous)
        raise InvalidRunTransition(f"terminal run state {previous.value} rejects {trigger.value}")
    target = _GLOBAL_TRANSITIONS.get(trigger) or _TRANSITIONS.get((previous, trigger))
    if target is None:
        raise InvalidRunTransition(f"run state {previous.value} rejects {trigger.value}")
    return RunTransition(previous, trigger, target)

"""RunMailbox 的持久化领取契约。

邮箱只保存“有一条已进入 Context 数据域的新输入”这一最小事实及其类型，不复制用户正文。
Graph 在安全边界领取消息后重新读取 Context，由 Context 保持原始消息、ACL 与保留期所有权。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any, Protocol


class RunMailboxInputType(StrEnum):
    """统一 Agent Inbox 的输入类型；不同类型只能在其允许的安全边界生效。"""

    USER = "user"
    STEERING = "steering"
    FOLLOW_UP = "follow_up"
    APPROVAL_RESULT = "approval_result"
    SUBAGENT_REPORT = "subagent_report"
    TEMPORAL_SIGNAL = "temporal_signal"
    SYSTEM_CONTEXT = "system_context"
    SCHEDULE_EVENT = "schedule_event"

    @property
    def requires_context_reload(self) -> bool:
        """标记该输入是否必须重新读取 Context 后再由 Planner 决定下一步。"""
        return self in {self.USER, self.STEERING, self.FOLLOW_UP, self.SYSTEM_CONTEXT}


class AgentInputPriority(IntEnum):
    """Inbox 的固定优先级；数值越小越先被同一 Run 的安全点领取。"""

    EMERGENCY = 0
    IMMEDIATE = 10
    NORMAL = 50
    DEFERRED = 90


@dataclass(frozen=True)
class ClaimedRunMailboxItem:
    """被单个 Graph Worker 临时领取的邮箱项，租约令牌防止重复确认。"""

    message_id: str
    input_type: RunMailboxInputType
    lease_token: str
    priority: AgentInputPriority = AgentInputPriority.NORMAL
    control_input: dict[str, Any] = field(default_factory=dict)


class RunMailbox(Protocol):
    """Graph 使用的窄邮箱接口，避免它依赖 RuntimeStore 或 SQL 实现细节。"""

    def claim_mailbox_input(
        self, tenant_id: str, run_id: str
    ) -> ClaimedRunMailboxItem | None:
        """领取当前 Run 最早的未消费输入；相同项在租约有效期内不会被第二个 Worker 领取。"""
        ...

    def acknowledge_mailbox_input(self, message_id: str, lease_token: str) -> bool:
        """仅在 Context 已成功重新装配后确认输入，失败时应让租约自然到期以便恢复。"""
        ...

    def has_pending_replan_input(self, tenant_id: str, run_id: str) -> bool:
        """检查是否有会改变计划的未领取输入；副作用屏障只读检查，绝不抢占租约。"""
        ...

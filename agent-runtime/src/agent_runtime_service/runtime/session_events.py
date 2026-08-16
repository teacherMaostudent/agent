"""Runtime Session 的追加式生命周期事件契约。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from platform_sdk.contracts.execution import ExecutionContext
from pydantic import BaseModel, Field


class RuntimeEventType(StrEnum):
    """运行状态提交后可写入 Session 事件流的固定事件类型。"""

    RUN_STARTED = "runtime.run.started"
    RUN_WAITING_APPROVAL = "runtime.run.waiting_approval"
    RUN_COMPLETED = "runtime.run.completed"
    RUN_FAILED = "runtime.run.failed"
    RUN_CANCEL_REQUESTED = "runtime.run.cancel_requested"


class RuntimeLifecycleEvent(BaseModel):
    """同一会话内严格递增的无敏感正文事件，可用于审计关联与确定性回放。"""

    event_id: str
    sequence: int = Field(ge=1)
    event_type: RuntimeEventType
    occurred_at: datetime
    run_id: str
    parent_run_id: str = ""
    trace_id: str
    tenant_id: str
    agent_id: str
    snapshot_id: str
    session_id: str
    status: str
    error_code: str = ""
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @classmethod
    def from_execution(
        cls,
        context: ExecutionContext,
        event_type: RuntimeEventType,
        *,
        sequence: int,
        status: str,
        error_code: str = "",
        metadata: Mapping[str, str | int | float | bool] | None = None,
    ) -> RuntimeLifecycleEvent:
        """从已发布的执行上下文构造事件；事件 ID 与会话序号由 Store 统一分配。"""
        return cls(
            event_id=f"rse_{uuid4().hex}",
            sequence=sequence,
            event_type=event_type,
            occurred_at=datetime.now(UTC),
            run_id=context.run_id,
            parent_run_id=context.parent_run_id,
            trace_id=context.trace_id,
            tenant_id=context.tenant_id,
            agent_id=context.agent_id,
            snapshot_id=context.snapshot_id,
            session_id=context.session_id,
            status=status,
            error_code=error_code,
            metadata=dict(metadata or {}),
        )


def event_type_for_status(status: str) -> RuntimeEventType:
    """将 Runtime 持久化状态收敛为单一生命周期事件类型，禁止各调用点自行猜测。"""
    if status == "WAITING_APPROVAL":
        return RuntimeEventType.RUN_WAITING_APPROVAL
    if status == "FAILED":
        return RuntimeEventType.RUN_FAILED
    return RuntimeEventType.RUN_COMPLETED

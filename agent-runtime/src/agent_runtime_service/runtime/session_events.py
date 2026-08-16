"""Runtime Session 的追加式生命周期事件契约。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from platform_sdk.contracts.execution import ExecutionContext
from platform_sdk.security import redact_text
from pydantic import BaseModel, Field


class RuntimeEventType(StrEnum):
    """运行状态提交后可写入 Session 事件流的固定事件类型。"""

    RUN_STARTED = "runtime.run.started"
    RUN_WAITING_APPROVAL = "runtime.run.waiting_approval"
    RUN_COMPLETED = "runtime.run.completed"
    RUN_FAILED = "runtime.run.failed"
    RUN_CANCEL_REQUESTED = "runtime.run.cancel_requested"
    TURN_STARTED = "runtime.turn.started"
    STEP_STARTED = "runtime.step.started"
    PROMPT_ASSEMBLED = "runtime.prompt.assembled"
    MODEL_REQUESTED = "runtime.model.requested"
    MODEL_RESPONDED = "runtime.model.responded"
    TOOL_CALLED = "runtime.tool.called"
    TOOL_RESULT = "runtime.tool.result"
    SUBAGENT_DELEGATED = "runtime.subagent.delegated"
    SUBAGENT_RESULT = "runtime.subagent.result"
    USER_MESSAGE = "runtime.user.message"
    ASSISTANT_MESSAGE = "runtime.assistant.message"


class ModelVisibleMessage(BaseModel):
    """写入 Session 的受限模型消息投影，不保存未经处理的敏感正文。"""

    role: Literal["user", "assistant", "tool", "system"]
    content: str = Field(max_length=16_000)
    content_sha256: str
    source: str = Field(max_length=80)
    redacted: bool = True
    truncated: bool = False


def model_visible_message(
    role: Literal["user", "assistant", "tool", "system"],
    content: str,
    *,
    source: str,
    max_chars: int = 12_000,
) -> ModelVisibleMessage:
    """生成可审计的脱敏消息投影，并以原文摘要证明它对应的输入版本。

    原始正文仍由 Context、RAG 或 Tool Gateway 在各自数据域内保管。Runtime 只持久化
    可重建决策 Prompt 的受限投影，避免执行日志成为新的明文敏感数据副本。
    """
    raw = str(content)
    bounded = raw[:max_chars]
    return ModelVisibleMessage(
        role=role,
        content=redact_text(bounded),
        content_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        source=source,
        truncated=len(raw) > max_chars,
    )


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
    metadata: dict[str, Any] = Field(default_factory=dict)
    # 只有经过脱敏与上限控制的投影可进入这个字段；原始敏感正文不得通过 Runtime Event API 写入。
    model_message: ModelVisibleMessage | None = None

    @classmethod
    def from_execution(
        cls,
        context: ExecutionContext,
        event_type: RuntimeEventType,
        *,
        sequence: int,
        status: str,
        error_code: str = "",
        metadata: Mapping[str, Any] | None = None,
        model_message: ModelVisibleMessage | None = None,
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
            model_message=model_message,
        )


def derive_model_messages(events: Iterable[RuntimeLifecycleEvent]) -> list[dict[str, str]]:
    """从 Session 事件流恢复模型可见消息投影，拒绝读取未声明为模型可见的元数据。"""
    messages: list[dict[str, str]] = []
    for event in events:
        if event.model_message is None:
            continue
        messages.append(
            {"role": event.model_message.role, "content": event.model_message.content}
        )
    return messages


def event_type_for_status(status: str) -> RuntimeEventType:
    """将 Runtime 持久化状态收敛为单一生命周期事件类型，禁止各调用点自行猜测。"""
    if status == "WAITING_APPROVAL":
        return RuntimeEventType.RUN_WAITING_APPROVAL
    if status == "FAILED":
        return RuntimeEventType.RUN_FAILED
    return RuntimeEventType.RUN_COMPLETED

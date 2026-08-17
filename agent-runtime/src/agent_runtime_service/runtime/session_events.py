"""Runtime Session 的追加式生命周期事件契约。"""

from __future__ import annotations

import hashlib
import json
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

    SESSION_CREATED = "runtime.session.created"
    SESSION_FORKED = "runtime.session.forked"
    SESSION_INTERRUPTED = "runtime.session.interrupted"
    SESSION_COMPACTED = "runtime.session.compacted"
    RUN_STARTED = "runtime.run.started"
    RUN_WAITING_APPROVAL = "runtime.run.waiting_approval"
    RUN_COMPLETED = "runtime.run.completed"
    RUN_FAILED = "runtime.run.failed"
    RUN_CANCEL_REQUESTED = "runtime.run.cancel_requested"
    TURN_STARTED = "runtime.turn.started"
    TURN_COMPLETED = "runtime.turn.completed"
    TURN_INTERRUPTED = "runtime.turn.interrupted"
    STEP_STARTED = "runtime.step.started"
    STEP_COMPLETED = "runtime.step.completed"
    STEP_FAILED = "runtime.step.failed"
    PROMPT_ASSEMBLED = "runtime.prompt.assembled"
    CONTEXT_INJECTED = "runtime.context.injected"
    REQUEST_EPOCH_PINNED = "runtime.request_epoch.pinned"
    MODEL_REQUESTED = "runtime.model.requested"
    MODEL_RESPONDED = "runtime.model.responded"
    TOOL_INTENT_RECORDED = "runtime.tool.intent_recorded"
    # 保留旧名称作为兼容别名；所有新代码应记录不可变副作用意图而非模糊的“调用”。
    TOOL_CALLED = "runtime.tool.intent_recorded"
    TOOL_EXECUTION_OBSERVED = "runtime.tool.execution_observed"
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


class SessionHeader(BaseModel):
    """一个长期会话的不可变归属与发布快照锚点。"""

    session_id: str
    tenant_id: str
    owner_id: str
    agent_id: str
    agent_version: str
    snapshot_id: str
    parent_session_id: str = ""
    seed_sequence: int = Field(default=0, ge=0)
    delegation_depth: int = Field(default=0, ge=0, le=32)
    retention_class: Literal["standard", "regulated"] = "standard"
    session_contract_version: str = "session-runtime/v1"
    created_at: datetime


class SessionProjection(BaseModel):
    """由追加事件派生的快速读取视图；可丢弃并从 Ledger 确定性重建。"""

    session_id: str
    tenant_id: str
    last_sequence: int = Field(default=0, ge=0)
    active_turn_id: str = ""
    active_step_id: str = ""
    status: str = "ACTIVE"
    surface_event_ids: list[str] = Field(default_factory=list)
    compacted_through_sequence: int = Field(default=0, ge=0)
    updated_at: datetime


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
    turn_id: str = ""
    step_id: str = ""
    epoch_id: str = ""
    attempt_id: str = ""
    payload_version: str = "session-event/v1"
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
        turn_id: str = "",
        step_id: str = "",
        epoch_id: str = "",
        attempt_id: str = "",
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
            turn_id=turn_id,
            step_id=step_id,
            epoch_id=epoch_id,
            attempt_id=attempt_id,
            status=status,
            error_code=error_code,
            metadata=dict(metadata or {}),
            model_message=model_message,
        )


def derive_model_messages(events: Iterable[RuntimeLifecycleEvent]) -> list[dict[str, str]]:
    """从 Session 事件流恢复当前模型 Surface，并应用追加式压缩替换规则。

    压缩不会修改历史事件；它仅让之后的 Surface 忽略被替代范围，并以新的摘要投影
    进入模型上下文。审计或取证仍可直接读取完整 Event Ledger。
    """
    messages: list[tuple[int, dict[str, str]]] = []
    for event in events:
        if event.event_type == RuntimeEventType.SESSION_COMPACTED:
            replaced_through = int(event.metadata.get("replaced_through_sequence", 0))
            messages = [item for item in messages if item[0] > replaced_through]
        if event.model_message is None:
            continue
        messages.append(
            (event.sequence, {"role": event.model_message.role, "content": event.model_message.content})
        )
    return [message for _, message in messages]


def derive_session_projection(
    header: SessionHeader, events: Iterable[RuntimeLifecycleEvent]
) -> SessionProjection:
    """从账本重放出会话当前状态，Projection 不得自行引入未经记录的业务事实。"""
    projection = SessionProjection(
        session_id=header.session_id,
        tenant_id=header.tenant_id,
        updated_at=header.created_at,
    )
    for event in events:
        projection.last_sequence = event.sequence
        projection.updated_at = event.occurred_at
        if event.event_type == RuntimeEventType.TURN_STARTED:
            projection.active_turn_id = event.turn_id
            projection.status = "RUNNING"
        elif event.event_type in {
            RuntimeEventType.TURN_COMPLETED,
            RuntimeEventType.TURN_INTERRUPTED,
        }:
            projection.active_turn_id = ""
            projection.active_step_id = ""
            projection.status = "INTERRUPTED" if event.event_type == RuntimeEventType.TURN_INTERRUPTED else "ACTIVE"
        elif event.event_type == RuntimeEventType.STEP_STARTED:
            projection.active_step_id = event.step_id
        elif event.event_type in {RuntimeEventType.STEP_COMPLETED, RuntimeEventType.STEP_FAILED}:
            projection.active_step_id = ""
        elif event.event_type == RuntimeEventType.SESSION_COMPACTED:
            projection.compacted_through_sequence = int(
                event.metadata.get("replaced_through_sequence", 0)
            )
        if event.model_message is not None:
            projection.surface_event_ids.append(event.event_id)
    return projection


def deterministic_tool_execution_id(
    run_id: str, step_id: str, tool_name: str, arguments: Mapping[str, Any]
) -> str:
    """用运行、Step、工具版本前参数生成稳定执行 ID，重放同一副作用必然复用该标识。"""
    canonical = json.dumps(
        {"run_id": run_id, "step_id": step_id, "tool_name": tool_name, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"tex_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]}"


def event_type_for_status(status: str) -> RuntimeEventType:
    """将 Runtime 持久化状态收敛为单一生命周期事件类型，禁止各调用点自行猜测。"""
    if status == "WAITING_APPROVAL":
        return RuntimeEventType.RUN_WAITING_APPROVAL
    if status == "FAILED":
        return RuntimeEventType.RUN_FAILED
    return RuntimeEventType.RUN_COMPLETED

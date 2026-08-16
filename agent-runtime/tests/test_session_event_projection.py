from datetime import UTC, datetime, timedelta

from platform_sdk.contracts.execution import ExecutionContext

from agent_runtime_service.runtime.integration import RuntimeStore
from agent_runtime_service.runtime.session_events import (
    RuntimeEventType,
    derive_model_messages,
    model_visible_message,
)


def _context() -> ExecutionContext:
    """构造同一会话的确定性上下文，验证追加事件不依赖 HTTP 或 Graph。"""
    return ExecutionContext(
        request_id="request-1",
        trace_id="trace-1",
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        agent_id="agent-1",
        agent_version="agent-1:1",
        snapshot_id="snapshot-1",
        graph_version="graph-1",
        model_policy_version="model-1",
        run_id="run-1",
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        attempt_budget_remaining=3,
    )


def test_model_visible_events_are_redacted_bounded_and_derivable(tmp_path) -> None:
    """Session 仅保留受限投影，但可按顺序重建模型看到的消息序列。"""
    store = RuntimeStore(tmp_path / "runtime.db")
    context = _context()
    store.create_with_session_event(context)
    store.append_session_event(
        context,
        RuntimeEventType.USER_MESSAGE,
        model_message=model_visible_message(
            "user", "api_key=top-secret@example.com", source="test"
        ),
    )
    events = store.session_events("tenant-1", "session-1")

    assert [event.sequence for event in events] == [1, 2]
    messages = derive_model_messages(events)
    assert messages == [{"role": "user", "content": "api_key=[REDACTED]"}]
    assert events[-1].model_message is not None
    assert events[-1].model_message.content_sha256
    store.close()

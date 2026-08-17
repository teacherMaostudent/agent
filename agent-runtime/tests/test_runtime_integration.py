from __future__ import annotations

from platform_sdk.contracts.execution import ExecutionContext

from agent_runtime_service.runtime.integration import GovernanceOutboxPublisher, RuntimeStore
from agent_runtime_service.runtime.session_events import RuntimeEventType


def _context() -> ExecutionContext:
    return ExecutionContext.create(
        request_id="request-1",
        trace_id="trace-1",
        session_id="session-1",
        tenant_id="tenant-a",
        user_id="user-a",
        agent_id="general-agent",
        agent_version="1.2.3",
        snapshot_id="version-abc",
        deadline_seconds=30,
        attempt_budget=4,
    )


def test_runtime_store_persists_run_and_governance_outbox(tmp_path) -> None:
    database = tmp_path / "runtime.db"
    context = _context()
    store = RuntimeStore(database)
    store.create(context)
    store.finish(context.run_id, "COMPLETED", {"steps": 2, "evidence": [{"id": "e1"}]})
    publisher = GovernanceOutboxPublisher(store, "", "", 1)
    publisher.publish_run(context, "COMPLETED", {"steps": 2, "evidence": [{"id": "e1"}]})
    store.close()

    reopened = RuntimeStore(database)
    run = reopened.get("tenant-a", context.run_id)
    events = reopened.pending_events()

    assert run is not None
    assert run.status == "COMPLETED"
    assert run.context.snapshot_id == "version-abc"
    assert len(events) == 1
    assert events[0]["event_type"] == "agent.run.completed"
    assert events[0]["trace_id"] == "trace-1"
    reopened.mark_delivered(events[0]["event_id"])
    assert reopened.pending_events() == []
    reopened.close()


def test_runtime_store_cancel_is_tenant_scoped(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.db")
    context = _context()
    store.create(context)

    assert store.cancel("other-tenant", context.run_id) is None
    cancelled = store.cancel("tenant-a", context.run_id)

    assert cancelled is not None
    assert cancelled.cancel_requested is True
    store.close()


def test_runtime_store_replays_request_id_and_atomically_enqueues_completion(
    tmp_path,
) -> None:
    store = RuntimeStore(tmp_path / "runtime.db")
    context = _context()
    created = store.create(context)
    replay = store.create(context.model_copy(update={"run_id": "run-other"}))

    assert replay.run_id == created.run_id

    publisher = GovernanceOutboxPublisher(store, "", "", 1)
    result = {
        "steps": 1,
        "evidence": [],
        "execution_plan": {"intent": {"name": "knowledge_query"}},
        "budget": {"spent_cost_usd": 0.01},
    }
    event = publisher.event_for_run(context, "COMPLETED", result)
    store.finish_and_enqueue(context.run_id, "COMPLETED", result, event)

    persisted = store.get("tenant-a", context.run_id)
    assert persisted is not None
    assert persisted.status == "COMPLETED"
    assert store.pending_events()[0]["payload"]["intent"] == "knowledge_query"
    store.close()


def test_runtime_store_appends_replayable_session_events_in_same_state_transactions(tmp_path) -> None:
    """开始、取消和终态与 Run 状态同一事务提交，并形成单调会话序号。"""
    store = RuntimeStore(tmp_path / "runtime.db")
    context = _context()
    store.create(context)
    store.cancel("tenant-a", context.run_id)
    publisher = GovernanceOutboxPublisher(store, "", "", 1)
    event = publisher.event_for_run(context, "COMPLETED", {"steps": 3})
    store.finish_and_enqueue(context.run_id, "COMPLETED", {"steps": 3}, event)

    events = store.session_events("tenant-a", "session-1")

    assert [item.sequence for item in events] == [1, 2, 3, 4]
    assert [item.event_type.value for item in events] == [
        "runtime.session.created",
        "runtime.run.started",
        "runtime.run.cancel_requested",
        "runtime.run.completed",
    ]
    assert events[-1].metadata == {"steps": 3}
    assert store.session_events("tenant-a", "session-1", after_sequence=3) == [events[-1]]
    store.close()


def test_runtime_store_detects_unresolved_tool_intent_for_recovery(tmp_path) -> None:
    """写工具先落 Intent；只有同一执行标识的结果事实才能解除恢复前的对账要求。"""
    store = RuntimeStore(tmp_path / "runtime.db")
    context = _context()
    store.create(context)
    store.append_session_event(
        context,
        RuntimeEventType.TOOL_INTENT_RECORDED,
        metadata={
            "tool_name": "write_record",
            "tool_execution_id": "tex_001",
            "idempotency_key": "tex_001",
        },
    )
    assert len(store.unresolved_tool_intents("tenant-a", "session-1", context.run_id)) == 1
    store.append_session_event(
        context,
        RuntimeEventType.TOOL_RESULT,
        metadata={"tool_execution_id": "tex_001", "success": True},
    )
    assert store.unresolved_tool_intents("tenant-a", "session-1", context.run_id) == []
    store.close()

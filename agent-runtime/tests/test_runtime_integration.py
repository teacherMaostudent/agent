from __future__ import annotations

from app.contracts.execution import ExecutionContext

from agent_runtime_service.runtime.integration import GovernanceOutboxPublisher, RuntimeStore


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

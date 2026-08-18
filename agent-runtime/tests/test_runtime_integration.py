from __future__ import annotations

from platform_sdk.contracts.execution import ExecutionContext

from agent_runtime_service.runtime.integration import GovernanceOutboxPublisher, RuntimeStore
from agent_runtime_service.runtime.mailbox import RunMailboxInputType
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


def test_execution_context_defaults_root_task_to_generated_run_id() -> None:
    """未传 run_id 的根请求也必须有稳定 Root Task，供跨 Agent 预算与取消树关联。"""
    context = _context()

    assert context.root_task_id == context.run_id


def test_runtime_store_cancel_is_tenant_scoped(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.db")
    context = _context()
    store.create(context)

    assert store.cancel("other-tenant", context.run_id) is None
    cancelled = store.cancel("tenant-a", context.run_id)

    assert cancelled is not None
    assert cancelled.cancel_requested is True
    cancellation_event = store.pending_events()[0]
    assert cancellation_event["event_type"] == "agent.run.state_changed"
    assert cancellation_event["payload"]["previous_runtime_state"] == "CREATED"
    assert cancellation_event["payload"]["runtime_state"] == "CANCELLED"
    store.close()


def test_runtime_store_cancellation_tree_marks_descendant_runs(tmp_path) -> None:
    """取消根运行必须覆盖已持久化的子谱系，防止子 Agent 成为继续执行的孤儿。"""
    store = RuntimeStore(tmp_path / "runtime.db")
    root = _context()
    child = _context().model_copy(
        update={
            "request_id": "request-child",
            "run_id": "run-child",
            "session_id": "session-child",
            "parent_run_id": root.run_id,
            "parent_session_id": root.session_id,
        }
    )
    store.create(root)
    store.create(child)

    cancelled = store.cancel_tree("tenant-a", root.run_id)

    assert {run.run_id for run in cancelled} == {root.run_id, child.run_id}
    assert store.get("tenant-a", child.run_id).cancel_requested is True
    store.close()


def test_root_budget_ledger_is_shared_idempotent_and_never_overcommits(tmp_path) -> None:
    """并发子 Agent 的额度必须在同一 Root 账本原子预留，而非各自复制父预算。"""
    store = RuntimeStore(tmp_path / "runtime.db")
    store.initialize_root_budget("tenant-a", "root-1", max_cost_usd=1.0, max_steps=2)

    store.reserve_root_budget(
        "tenant-a", "root-1", "run-parent", "reservation-1", cost_usd=0.6, steps=1
    )
    # Activity 重放使用同一 ID，不得把已经预留的额度再算一次。
    store.reserve_root_budget(
        "tenant-a", "root-1", "run-parent", "reservation-1", cost_usd=0.6, steps=1
    )
    store.settle_root_budget("reservation-1", actual_cost_usd=0.4, actual_steps=1)
    store.reserve_root_budget(
        "tenant-a", "root-1", "run-child", "reservation-2", cost_usd=0.6, steps=1
    )

    try:
        store.reserve_root_budget(
            "tenant-a", "root-1", "run-child", "reservation-3", cost_usd=0.1, steps=1
        )
    except Exception as exc:
        assert getattr(exc, "code", "") == "ROOT_BUDGET_EXCEEDED"
    else:  # pragma: no cover - failure branch makes the budget check observable.
        raise AssertionError("shared root budget must reject overcommit")
    store.close()


def test_approval_control_input_is_persisted_and_leased_without_message_body(tmp_path) -> None:
    """审批决定使用 Inbox 的受限控制载荷，避免只存在于 API 进程或 Temporal Signal。"""
    store = RuntimeStore(tmp_path / "runtime.db")
    context = _context()
    store.create(context)
    message_id = store.enqueue_mailbox_input(
        "tenant-a",
        context.run_id,
        RunMailboxInputType.APPROVAL_RESULT,
        idempotency_key="approval-1",
        control_input={"approved": True, "approval_id": "approval-1", "payload_version": "v1"},
    )

    claimed = store.claim_mailbox_input("tenant-a", context.run_id)

    assert claimed is not None
    assert claimed.message_id == message_id
    assert claimed.input_type is RunMailboxInputType.APPROVAL_RESULT
    assert claimed.control_input == {
        "approved": True,
        "approval_id": "approval-1",
        "payload_version": "v1",
    }
    assert store.acknowledge_mailbox_input(message_id, claimed.lease_token) is True
    store.close()


def test_governance_marks_waiting_for_user_input_as_interrupted(tmp_path) -> None:
    """澄清中断不是完成结果，治理侧必须与审批中断使用同类事件表达。"""
    store = RuntimeStore(tmp_path / "runtime.db")
    event = GovernanceOutboxPublisher(store, "", "", 1).event_for_run(
        _context(), "WAITING_INPUT", {"steps": 1}
    )

    assert event["event_type"] == "agent.run.interrupted"
    assert event["payload"]["status"] == "WAITING_INPUT"
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
    events = store.pending_events()
    assert {item["event_type"] for item in events} == {
        "agent.run.state_changed",
        "agent.run.completed",
    }
    completion = next(item for item in events if item["event_type"] == "agent.run.completed")
    assert completion["payload"]["intent"] == "knowledge_query"
    store.close()


def test_runtime_store_appends_replayable_session_events_in_same_state_transactions(tmp_path) -> None:
    """取消状态一旦提交就不能被迟到的成功终态覆盖，账本序号仍保持单调。"""
    store = RuntimeStore(tmp_path / "runtime.db")
    context = _context()
    store.create(context)
    store.cancel("tenant-a", context.run_id)
    events = store.session_events("tenant-a", "session-1")

    assert [item.sequence for item in events] == [1, 2, 3]
    assert [item.event_type.value for item in events] == [
        "runtime.session.created",
        "runtime.run.started",
        "runtime.run.cancel_requested",
    ]
    assert events[-1].metadata["current_state"] == "CANCELLED"
    assert store.session_events("tenant-a", "session-1", after_sequence=2) == [events[-1]]
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

from datetime import UTC, datetime, timedelta

from platform_sdk.contracts.execution import ExecutionContext

from agent_runtime_service.runtime.event_bus import (
    RuntimeEventBus,
    RuntimeEventType,
    RuntimeLifecycleEvent,
)


def _context() -> ExecutionContext:
    """构造固定关联 ID 的运行上下文，验证事件不需要读取任务正文。"""
    return ExecutionContext(
        request_id="request-a",
        trace_id="trace-a",
        session_id="session-a",
        tenant_id="tenant-a",
        user_id="user-a",
        agent_id="agent-a",
        agent_version="agent-a:1.0.0",
        snapshot_id="snapshot-a",
        graph_version="graph-a",
        model_policy_version="policy-a",
        run_id="run-a",
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        attempt_budget_remaining=3,
    )


def test_event_bus_delivers_only_matching_frozen_lifecycle_subscription() -> None:
    """事件只通知启动时声明的同类订阅者，不存在请求期间动态安装回调的入口。"""
    received: list[RuntimeLifecycleEvent] = []
    bus = RuntimeEventBus({RuntimeEventType.RUN_COMPLETED: (received.append,)})

    bus.publish(
        RuntimeLifecycleEvent.from_execution(
            _context(), RuntimeEventType.RUN_STARTED, sequence=1, status="RUNNING"
        )
    )
    event = RuntimeLifecycleEvent.from_execution(
        _context(), RuntimeEventType.RUN_COMPLETED, sequence=2, status="COMPLETED", metadata={"steps": 2}
    )
    bus.publish(event)

    assert received == [event]
    assert received[0].tenant_id == "tenant-a"
    assert received[0].metadata == {"steps": 2}


def test_event_subscriber_failure_does_not_reverse_committed_runtime_state() -> None:
    """本地观测回调失败只能记录异常，可靠审计仍交给事务 Outbox。"""
    bus = RuntimeEventBus(
        {RuntimeEventType.RUN_FAILED: (lambda _: (_ for _ in ()).throw(RuntimeError("sink")),)}
    )

    bus.publish(
        RuntimeLifecycleEvent.from_execution(
            _context(), RuntimeEventType.RUN_FAILED, sequence=1, status="FAILED", error_code="RuntimeError"
        )
    )

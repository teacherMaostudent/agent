"""Runtime 工具调度的并发与资源隔离测试。"""

from __future__ import annotations

from threading import Event

from agent_runtime_service.runtime.tool_execution import (
    ScheduledToolCall,
    ToolExecutionEngine,
    ToolExecutionPolicy,
    ToolSchedulingMode,
)


def test_parallel_batch_runs_independent_read_calls_without_serializing() -> None:
    """只读并行模式允许两个独立调用共同开始，避免无意义地拉长 RAG/扫描等待。"""
    engine = ToolExecutionEngine()
    first_started, second_started = Event(), Event()

    def first() -> str:
        first_started.set()
        assert second_started.wait(timeout=1)
        return "first"

    def second() -> str:
        second_started.set()
        assert first_started.wait(timeout=1)
        return "second"

    result = engine.execute_batch(
        (
            (
                ScheduledToolCall("c1", "scan", ToolSchedulingMode.PARALLEL, "tenant:scan"),
                first,
            ),
            (
                ScheduledToolCall("c2", "search", ToolSchedulingMode.PARALLEL, "tenant:search"),
                second,
            ),
        )
    )
    assert result == ["first", "second"]


def test_sequential_call_requires_nonempty_resource_key() -> None:
    """写操作不得省略资源键，否则无法解释或约束同一业务资源上的并发。"""
    engine = ToolExecutionEngine()
    try:
        engine.execute(
            ScheduledToolCall("c1", "write", ToolSchedulingMode.EXCLUSIVE, ""), lambda: "never"
        )
    except ValueError as exc:
        assert "resource key" in str(exc)
    else:
        raise AssertionError("exclusive tool call must require a resource key")


def test_published_tool_policy_keeps_high_risk_writes_exclusive_and_explainable() -> None:
    """模型不能将高风险工具改成并行调用；策略事实可安全进入 Session Ledger。"""
    policy = ToolExecutionPolicy.from_published_binding(
        {
            "tool_name": "refund",
            "risk": "write_high_risk",
            "approval_required": True,
            "idempotent": True,
            "resource_key": "order:123",
        },
        tenant_id="tenant-a",
        tool_name="refund",
    )

    scheduled = policy.scheduled_call(call_id="call-1", tool_name="refund")
    facts = ToolExecutionEngine.policy_facts(policy)

    assert scheduled.mode == ToolSchedulingMode.EXCLUSIVE
    assert scheduled.resource_key == "order:123"
    assert facts == {
        "scheduling_mode": "exclusive",
        "resource_key": "order:123",
        "side_effect": True,
        "idempotent": True,
        "approval_required": True,
    }

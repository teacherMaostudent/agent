"""执行内核的状态机与 Provider Registry 回归测试。"""

from __future__ import annotations

import pytest
from platform_sdk.contracts.execution import ExecutionContext

from agent_runtime_service.runtime.catalog import ExecutionProvider, ExecutionProviderRegistry
from agent_runtime_service.runtime.event_bus import RuntimeEventBus
from agent_runtime_service.runtime.harness import SimpleExecutor
from agent_runtime_service.runtime.integration import RuntimeStore
from agent_runtime_service.runtime.mailbox import AgentInputPriority, RunMailboxInputType
from agent_runtime_service.runtime.models import (
    ExecutionLifecycle,
    ExecutionMode,
    ExecutionRequirements,
    ReasoningMode,
)
from agent_runtime_service.runtime.run_state import (
    AgentRunEvent,
    AgentRunState,
    InvalidRunTransition,
    transition_run_state,
)


def _context() -> ExecutionContext:
    """构造带固定关联身份的最小运行，避免状态机测试依赖 Graph 或网络。"""
    return ExecutionContext.create(
        request_id="request-state",
        trace_id="trace-state",
        session_id="session-state",
        tenant_id="tenant-state",
        user_id="user-state",
        agent_id="agent-state",
        agent_version="1.0.0",
        snapshot_id="snapshot-state",
        deadline_seconds=60,
        attempt_budget=3,
    )


def test_state_machine_requires_declared_path_and_rejects_terminal_revival() -> None:
    """状态机接受准备到工具执行的合法链，拒绝跳步和取消后的迟到成功。"""
    state = AgentRunState.CREATED
    for event, expected in (
        (AgentRunEvent.START, AgentRunState.PREPARING_CONTEXT),
        (AgentRunEvent.CONTEXT_READY, AgentRunState.REQUESTING_MODEL),
        (AgentRunEvent.TOOL_INTENT_RECORDED, AgentRunState.EXECUTING_TOOLS),
        (AgentRunEvent.TOOLS_COMPLETED, AgentRunState.REQUESTING_MODEL),
        (AgentRunEvent.CANCEL_REQUESTED, AgentRunState.CANCELLED),
    ):
        state = transition_run_state(state, event).current
        assert state == expected
    with pytest.raises(InvalidRunTransition, match="terminal"):
        transition_run_state(state, AgentRunEvent.RUN_COMPLETED)


def test_store_persists_state_transition_and_does_not_overwrite_cancelled_run(tmp_path) -> None:
    """状态更新与账本同事务提交，取消后不得以最终结果重新激活 Run。"""
    store = RuntimeStore(tmp_path / "runtime.db")
    context = _context()
    store.create(context)
    run, event = store.transition_state(
        context.tenant_id, context.run_id, AgentRunEvent.START
    )
    assert run is not None and run.runtime_state == AgentRunState.PREPARING_CONTEXT
    assert event is not None and event.metadata["trigger"] == AgentRunEvent.START
    governance_events = store.pending_events()
    assert len(governance_events) == 1
    assert governance_events[0]["event_type"] == "agent.run.state_changed"
    assert governance_events[0]["trace_id"] == context.trace_id
    assert governance_events[0]["payload"] == {
        "run_id": context.run_id,
        "session_id": context.session_id,
        "agent_id": context.agent_id,
        "snapshot_id": context.snapshot_id,
        "agent_version": context.agent_version,
        "status": "RUNNING",
        "runtime_state": AgentRunState.PREPARING_CONTEXT.value,
        "previous_runtime_state": AgentRunState.CREATED.value,
        "transition_event": AgentRunEvent.START.value,
        "session_event_id": event.event_id,
        "sequence": event.sequence,
    }
    store.cancel(context.tenant_id, context.run_id)
    with pytest.raises(InvalidRunTransition, match="terminal"):
        store.finish(context.run_id, "COMPLETED", {"steps": 1})
    persisted = store.get(context.tenant_id, context.run_id)
    assert persisted is not None and persisted.runtime_state == AgentRunState.CANCELLED
    store.close()


def test_provider_registry_exposes_mode_without_exposing_executor_in_api_shape() -> None:
    """Profile 与语义模式绑定在启动目录，运行请求只能解析已部署 Provider。"""
    registry = ExecutionProviderRegistry(
        {
            "fast/v1": ExecutionProvider(
                profile="fast/v1", mode=ExecutionMode.FAST, executor=SimpleExecutor(), supports_resume=False
            )
        }
    )
    assert registry.provider("fast/v1").mode == ExecutionMode.FAST
    assert registry.resolve("fast/v1").run({"task": "ok"}, "thread").status == "COMPLETED"
    with pytest.raises(LookupError, match="not deployed"):
        registry.resolve("unknown/v1")


def test_provider_registry_resolves_lifecycle_and_reasoning_as_two_dimensions() -> None:
    """相同 Graph 推理可以以请求型或 Durable 方式部署，目录必须拒绝歧义组合。"""
    request_executor = SimpleExecutor()
    durable_executor = SimpleExecutor()
    registry = ExecutionProviderRegistry(
        {
            "request/minimal": ExecutionProvider(
                profile="request/minimal",
                mode=ExecutionMode.FAST,
                executor=request_executor,
                lifecycle=ExecutionLifecycle.REQUEST_SCOPED,
                reasoning=ReasoningMode.MINIMAL,
            ),
            "durable/minimal": ExecutionProvider(
                profile="durable/minimal",
                mode=ExecutionMode.DURABLE,
                executor=durable_executor,
                lifecycle=ExecutionLifecycle.DURABLE_WORKFLOW,
                reasoning=ReasoningMode.MINIMAL,
            ),
        }
    )

    assert registry.resolve_requirements(
        ExecutionRequirements(
            lifecycle=ExecutionLifecycle.DURABLE_WORKFLOW,
            reasoning=ReasoningMode.MINIMAL,
        )
    ) is durable_executor


def test_event_projector_has_explicit_startup_registration_lifecycle() -> None:
    """投影可在启动期注册并关闭，冻结后不能由请求偷偷注入新的业务回调。"""
    from agent_runtime_service.runtime.session_events import RuntimeEventType

    bus = RuntimeEventBus()
    observed: list[str] = []
    handle = bus.register_projector(
        name="test-projection",
        event_type=RuntimeEventType.RUN_STARTED,
        subscriber=lambda event: observed.append(event.event_id),
    )
    handle.close()
    bus.freeze()
    with pytest.raises(RuntimeError, match="frozen"):
        bus.register_projector(
            name="late-projection",
            event_type=RuntimeEventType.RUN_STARTED,
            subscriber=lambda event: observed.append(event.event_id),
        )
    assert observed == []


def test_mailbox_uses_lease_then_ack_without_persisting_message_body(tmp_path) -> None:
    """邮箱只协调输入领取；正文留在 Context，租约未确认前不能被另一个 Worker 重复消费。"""
    store = RuntimeStore(tmp_path / "runtime.db")
    context = _context()
    store.create(context)
    message_id = store.enqueue_mailbox_input(
        context.tenant_id,
        context.run_id,
        RunMailboxInputType.STEERING,
        idempotency_key="input-1",
    )
    assert (
        store.enqueue_mailbox_input(
            context.tenant_id,
            context.run_id,
            RunMailboxInputType.STEERING,
            idempotency_key="input-1",
        )
        == message_id
    )
    claimed = store.claim_mailbox_input(context.tenant_id, context.run_id)
    assert claimed is not None and claimed.message_id == message_id
    assert store.claim_mailbox_input(context.tenant_id, context.run_id) is None
    assert store.acknowledge_mailbox_input(claimed.message_id, claimed.lease_token)
    assert store.claim_mailbox_input(context.tenant_id, context.run_id) is None
    store.close()


def test_agent_inbox_claims_priority_before_arrival_order(tmp_path) -> None:
    """紧急运行信号必须先于普通 Follow-up 被安全点领取，且正文仍不落 Runtime。"""
    store = RuntimeStore(tmp_path / "runtime.db")
    context = _context()
    store.create(context)
    store.enqueue_mailbox_input(
        context.tenant_id,
        context.run_id,
        RunMailboxInputType.FOLLOW_UP,
        idempotency_key="later",
    )
    urgent = store.enqueue_mailbox_input(
        context.tenant_id,
        context.run_id,
        RunMailboxInputType.SYSTEM_CONTEXT,
        idempotency_key="urgent",
        priority=AgentInputPriority.IMMEDIATE,
    )

    claimed = store.claim_mailbox_input(context.tenant_id, context.run_id)

    assert claimed is not None
    assert claimed.message_id == urgent
    assert claimed.priority == AgentInputPriority.IMMEDIATE
    assert claimed.input_type.requires_context_reload
    store.close()

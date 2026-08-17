from datetime import UTC, datetime, timedelta

import pytest
from platform_sdk.contracts.execution import ExecutionContext

from agent_runtime_service.runtime.integration import RuntimeStore
from agent_runtime_service.runtime.subagents import SubAgentManager, SubAgentPolicyError


def test_subagent_manager_allows_only_declared_bounded_delegation() -> None:
    """父快照决定目标、深度、次数和资源切分，模型不能自行扩张子 Agent 范围。"""
    delegation = SubAgentManager().prepare(
        [{"agent_id": "research-agent", "max_depth": 2, "max_budget_fraction": 0.25}],
        target_agent_id="research-agent",
        parent_depth=0,
        parent_remaining_steps=12,
        parent_remaining_cost_usd=4.0,
        prior_invocations=0,
    )

    assert delegation.depth == 1
    assert delegation.max_steps == 3
    assert delegation.max_cost_usd == 1.0


def test_subagent_manager_rejects_undeclared_or_exhausted_delegation() -> None:
    """不存在默认子 Agent，也不能通过重复调用绕过快照配置的调用上限。"""
    manager = SubAgentManager()
    with pytest.raises(SubAgentPolicyError, match="not declared"):
        manager.prepare([], target_agent_id="unknown", parent_depth=0, parent_remaining_steps=4,
                        parent_remaining_cost_usd=1.0, prior_invocations=0)
    with pytest.raises(SubAgentPolicyError, match="invocation limit"):
        manager.prepare([{"agent_id": "research-agent"}], target_agent_id="research-agent",
                        parent_depth=0, parent_remaining_steps=4, parent_remaining_cost_usd=1.0,
                        prior_invocations=1)


def test_runtime_store_authorizes_only_ancestor_subagent_lineage(tmp_path) -> None:
    """冷继续只允许祖先控制子运行，兄弟或后代不能反向操纵既有 Agent。"""
    store = RuntimeStore(tmp_path / "runtime.db")
    deadline = datetime.now(UTC) + timedelta(minutes=1)
    root = ExecutionContext(
        request_id="root-request", trace_id="trace", session_id="session", tenant_id="tenant",
        user_id="user", agent_id="root", agent_version="root:1", snapshot_id="snapshot",
        graph_version="graph", model_policy_version="model", run_id="root-run", deadline_at=deadline,
        attempt_budget_remaining=3,
    )
    child = root.model_copy(update={
        "request_id": "child-request", "run_id": "child-run", "agent_id": "child",
        "parent_run_id": "root-run",
        "session_id": "child-session",
        "parent_session_id": "session",
    })
    grandchild = child.model_copy(update={
        "request_id": "grandchild-request", "run_id": "grandchild-run", "parent_run_id": "child-run",
        "session_id": "grandchild-session",
        "parent_session_id": "child-session",
    })
    for context in (root, child, grandchild):
        store.create(context)

    assert store.is_run_ancestor("tenant", "root-run", "grandchild-run")
    assert store.is_run_ancestor("tenant", "child-run", "grandchild-run")
    assert not store.is_run_ancestor("tenant", "grandchild-run", "child-run")
    store.close()

import pytest

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

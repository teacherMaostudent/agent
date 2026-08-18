from __future__ import annotations

import pytest

from agent_runtime_service.runtime.agent_manager import AgentManager
from agent_runtime_service.runtime.subagents import SubAgentManager, SubAgentPolicyError


def _state() -> dict:
    """构造最小父运行状态，证明管理器只使用已编译的绑定和剩余预算。"""
    return {
        "agent_id": "parent",
        "max_steps": 12,
        "step_count": 1,
        "budget": {"max_cost_usd": 4.0, "spent_cost_usd": 0.0},
        "subagent_invocations": {},
        "metadata": {"_subagent_depth": 0},
        "compiled_plan": {
            "subagents": [
                {
                    "agent_id": "research",
                    "max_depth": 2,
                    "max_budget_fraction": 0.25,
                    "max_invocations": 1,
                }
            ]
        },
    }


def test_agent_manager_delegates_only_after_published_policy_validation() -> None:
    """Agent Manager 统一负责配额校验，Graph 不再接触委派调用器。"""
    seen: dict[str, object] = {}

    def execute(delegation, task: str, state: dict) -> dict:
        """记录子运行输入，模拟子 Harness 返回的受限摘要。"""
        seen.update({"delegation": delegation, "task": task, "state": state})
        return {"status": "COMPLETED", "budget": {"spent_cost_usd": 0.25}}

    delegation, result = AgentManager(SubAgentManager(), execute).delegate(
        _state(), target_agent_id="research", task="summarise evidence"
    )

    assert delegation.target_agent_id == "research"
    assert delegation.max_steps == 2
    assert result["status"] == "COMPLETED"
    assert seen["task"] == "summarise evidence"


def test_agent_manager_rejects_self_delegation_before_executor_is_called() -> None:
    """策略失败必须在调用子 Harness 前返回，防止递归和副作用泄漏。"""
    manager = AgentManager(SubAgentManager(), lambda *_: {"status": "COMPLETED"})

    with pytest.raises(SubAgentPolicyError, match="cannot delegate to itself"):
        manager.delegate(_state(), target_agent_id="parent", task="loop")

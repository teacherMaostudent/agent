"""由发布快照约束的子 Agent 委派配额管理。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from platform_sdk.contracts.subagents import SubAgentBinding


class SubAgentPolicyError(RuntimeError):
    """子 Agent 目标、深度或预算超出父快照授权时抛出。"""


@dataclass(frozen=True)
class SubAgentDelegation:
    """经验证的子任务配额，不包含权限扩张、模型路由或工具授权。"""

    target_agent_id: str
    depth: int
    max_steps: int
    max_cost_usd: float
    max_invocations: int
    delegated_permissions: frozenset[str] = frozenset()
    root_task_id: str = ""
    collaboration_snapshot_id: str = ""
    business_operation_id: str = ""


class SubAgentManager:
    """根据冻结计划准备子任务；实际运行仍须经子 Agent 自身 Release 与 Harness。"""

    def prepare(
        self,
        bindings: Iterable[dict],
        *,
        target_agent_id: str,
        parent_depth: int,
        parent_remaining_steps: int,
        parent_remaining_cost_usd: float,
        prior_invocations: int,
        parent_permissions: frozenset[str] = frozenset(),
        root_task_id: str = "",
        collaboration_snapshot_id: str = "",
        business_operation_id: str = "",
    ) -> SubAgentDelegation:
        """校验委派并切分父运行剩余配额，拒绝任意目标和无限递归。"""
        declared = [SubAgentBinding.model_validate(item) for item in bindings]
        binding = next((item for item in declared if item.agent_id == target_agent_id), None)
        if binding is None:
            raise SubAgentPolicyError("subagent is not declared in the published snapshot")
        depth = parent_depth + 1
        if depth > binding.max_depth:
            raise SubAgentPolicyError("subagent delegation depth exceeds published limit")
        if prior_invocations >= binding.max_invocations:
            raise SubAgentPolicyError("subagent invocation limit is exhausted")
        if (prior_invocations + 1) * binding.max_budget_fraction > 1:
            raise SubAgentPolicyError("subagent bindings would exceed the parent budget")
        max_steps = int(parent_remaining_steps * binding.max_budget_fraction)
        max_cost = parent_remaining_cost_usd * binding.max_budget_fraction
        if max_steps < 2 or max_cost <= 0:
            raise SubAgentPolicyError("parent runtime budget cannot fund a subagent")
        return SubAgentDelegation(
            target_agent_id=target_agent_id,
            depth=depth,
            max_steps=max_steps,
            max_cost_usd=max_cost,
            max_invocations=binding.max_invocations,
            # 未声明 delegated_permissions 等价于不给额外限制, 但始终不能超过父权限。
            delegated_permissions=(
                parent_permissions.intersection(binding.delegated_permissions)
                if binding.delegated_permissions
                else parent_permissions
            ),
            root_task_id=root_task_id,
            collaboration_snapshot_id=collaboration_snapshot_id,
            business_operation_id=business_operation_id,
        )

    def dispatch(
        self,
        state: dict[str, Any],
        *,
        target_agent_id: str,
        task: str,
        executor: Callable[[SubAgentDelegation, str, dict[str, Any]], dict[str, Any]],
        delegation_transform: Callable[[SubAgentDelegation], SubAgentDelegation] | None = None,
    ) -> tuple[SubAgentDelegation, dict[str, Any]]:
        """从冻结计划准备子任务并委派给内部调用器；调用器不得获得策略修改权。"""
        if target_agent_id == state.get("agent_id"):
            raise SubAgentPolicyError("an agent cannot delegate to itself")
        budget = state.get("budget", {})
        invocations = state.get("subagent_invocations", {})
        declared = [
            SubAgentBinding.model_validate(item)
            for item in state.get("compiled_plan", {}).get("subagents", [])
        ]
        reserved_fraction = sum(
            int(invocations.get(binding.agent_id, 0)) * binding.max_budget_fraction
            for binding in declared
        )
        target = next(
            (binding for binding in declared if binding.agent_id == target_agent_id), None
        )
        if target is not None and reserved_fraction + target.max_budget_fraction > 1:
            raise SubAgentPolicyError("all subagent delegations would exceed the parent budget")
        delegation = self.prepare(
            [binding.model_dump(mode="json") for binding in declared],
            target_agent_id=target_agent_id,
            parent_depth=int(state.get("metadata", {}).get("_subagent_depth", 0)),
            parent_remaining_steps=max(
                0, int(state.get("max_steps", 0)) - int(state.get("step_count", 0))
            ),
            parent_remaining_cost_usd=max(
                0.0, float(budget.get("max_cost_usd", 0)) - float(budget.get("spent_cost_usd", 0))
            ),
            prior_invocations=int(invocations.get(target_agent_id, 0)),
            parent_permissions=frozenset(str(item) for item in state.get("permissions", [])),
            root_task_id=str(state.get("root_task_id") or state.get("run_id", "")),
            collaboration_snapshot_id=str(state.get("collaboration_snapshot_id", "")),
            business_operation_id=str(
                state.get("business_operation_id") or state.get("run_id", "")
            ),
        )
        if delegation_transform is not None:
            delegation = delegation_transform(delegation)
        return delegation, executor(delegation, task, state)

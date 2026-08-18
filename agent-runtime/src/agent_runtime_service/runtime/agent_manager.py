"""多 Agent 委派的运行期管理边界。

Graph 只表达已发布执行计划中的状态流转。这个模块承接子 Agent 的冻结绑定
校验、预算切分与实际委派调用，避免把组织管理能力逐步塞进 LangGraph 节点。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from platform_sdk.contracts.subagents import CapabilityRequirement, SubAgentBinding

from agent_runtime_service.runtime.collaboration import (
    CapabilityRouter,
    CapabilitySelection,
    CollaborationError,
    structured_agent_result,
)
from agent_runtime_service.runtime.subagents import SubAgentDelegation, SubAgentManager


class AgentManager:
    """以单一门面执行已授权的子 Agent 委派。

    它不识别用户意图、不组装 Prompt、不选择模型或工具；这些职责分别属于
    Planner、Context/Decision Engine、LLM Gateway 与 Tool Gateway。这里仅确保
    每次委派都来自父快照的显式绑定，并将执行交给子 Agent 自身的 Harness。
    """

    def __init__(
        self,
        policy: SubAgentManager,
        executor: Callable[[SubAgentDelegation, str, dict[str, Any]], dict[str, Any]],
    ) -> None:
        """冻结委派策略与调用器，禁止 Graph 或请求路径替换执行目标。"""
        self._policy = policy
        self._executor = executor
        self._router = CapabilityRouter()

    def resolve_capability(
        self,
        state: dict[str, Any],
        requirement: CapabilityRequirement,
        *,
        unavailable_agents: frozenset[str] = frozenset(),
    ) -> CapabilitySelection:
        """从父 Snapshot 的协作目录选择能力 Provider，禁止 Planner 直接指定 Agent 实例。"""
        bindings = [
            SubAgentBinding.model_validate(item)
            for item in state.get("compiled_plan", {}).get("subagents", [])
        ]
        return self._router.select(
            bindings,
            requirement,
            caller_agent_id=str(state.get("agent_id", "")),
            unavailable_agents=unavailable_agents,
        )

    def resolve_capability_group(
        self,
        state: dict[str, Any],
        requirement: CapabilityRequirement,
        *,
        unavailable_agents: frozenset[str] = frozenset(),
    ) -> list[CapabilitySelection]:
        """解析一个能力的发布并行组，Graph 不得把 Provider 数量硬编码到节点中。"""
        bindings = [
            SubAgentBinding.model_validate(item)
            for item in state.get("compiled_plan", {}).get("subagents", [])
        ]
        return self._router.select_group(
            bindings,
            requirement,
            caller_agent_id=str(state.get("agent_id", "")),
            unavailable_agents=unavailable_agents,
        )

    def delegate(
        self,
        state: dict[str, Any],
        *,
        target_agent_id: str,
        task: str,
    ) -> tuple[SubAgentDelegation, dict[str, Any]]:
        """校验发布绑定和父预算后，调用目标 Agent 的独立运行入口。

        返回值只包含已冻结的子配额和子运行摘要；子 Agent 的模型、工具、知识域
        与审批规则仍须由其 own Release 重新解析，不能继承父图的内部对象。
        """
        return self._policy.dispatch(
            state,
            target_agent_id=target_agent_id,
            task=task,
            executor=self._executor,
        )

    def delegate_capability(
        self,
        state: dict[str, Any],
        *,
        requirement: CapabilityRequirement,
        task: str,
        unavailable_agents: frozenset[str] = frozenset(),
    ) -> tuple[CapabilitySelection, SubAgentDelegation, dict[str, Any]]:
        """解析已发布能力后复用既有配额委派，子 Agent 仍独立加载自己的 Release。"""
        selection = self.resolve_capability(
            state, requirement, unavailable_agents=unavailable_agents
        )
        # 本次选择与 Provider 的兼容契约写入稳定摘要; 重试/恢复不重新抽取 Canary。
        frozen = {
            "root_task_id": str(state.get("root_task_id") or state.get("run_id", "")),
            "capability_id": requirement.capability_id,
            "provider_agent_id": selection.binding.agent_id,
            "input_schema_version": selection.binding.input_schema_version,
            "output_schema_version": selection.binding.output_schema_version,
            "jurisdiction": selection.binding.jurisdiction,
        }
        snapshot_id = "collab_" + hashlib.sha256(
            json.dumps(frozen, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
        delegation, result = self._policy.dispatch(
            state,
            target_agent_id=selection.binding.agent_id,
            task=task,
            executor=self._executor,
            delegation_transform=lambda item: replace(
                item, collaboration_snapshot_id=snapshot_id
            ),
        )
        return selection, delegation, result

    def delegate_capability_group(
        self,
        state: dict[str, Any],
        *,
        requirement: CapabilityRequirement,
        task: str,
    ) -> list[tuple[CapabilitySelection, SubAgentDelegation, dict[str, Any]]]:
        """并行执行同一能力的已发布专家组，并在派发前验证总切分额度。

        每个子 Agent 仍通过自己的 Release/Harness 运行。线程池只并发等待独立运行
        结果，不共享 Planner、工具上下文或任何可变业务对象。
        """
        selections = self.resolve_capability_group(state, requirement)
        fraction_by_agent = {
            item.agent_id: item.max_budget_fraction
            for item in (
                SubAgentBinding.model_validate(binding)
                for binding in state.get("compiled_plan", {}).get("subagents", [])
            )
        }
        total_fraction = sum(fraction_by_agent[item.binding.agent_id] for item in selections)
        if total_fraction > 1:
            raise CollaborationError("parallel capability providers exceed the parent budget")

        def dispatch(selection: CapabilitySelection) -> tuple[CapabilitySelection, SubAgentDelegation, dict[str, Any]]:
            """为一个已选 Provider 冻结协作快照并运行，线程间不共享子执行状态。"""
            frozen = {
                "root_task_id": str(state.get("root_task_id") or state.get("run_id", "")),
                "capability_id": requirement.capability_id,
                "provider_agent_id": selection.binding.agent_id,
                "input_schema_version": selection.binding.input_schema_version,
                "output_schema_version": selection.binding.output_schema_version,
                "jurisdiction": selection.binding.jurisdiction,
            }
            snapshot_id = "collab_" + hashlib.sha256(
                json.dumps(frozen, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:24]
            delegation, result = self._policy.dispatch(
                state,
                target_agent_id=selection.binding.agent_id,
                task=task,
                executor=self._executor,
                delegation_transform=lambda item: replace(
                    item, collaboration_snapshot_id=snapshot_id
                ),
            )
            return selection, delegation, result

        with ThreadPoolExecutor(max_workers=len(selections), thread_name_prefix="agent-provider") as pool:
            return list(pool.map(dispatch, selections))

    def normalize_result(
        self, result: dict[str, Any], *, selection: CapabilitySelection | None, agent_id: str
    ):
        """输出跨 Agent 可仲裁结果；旧显式绑定没有权威元数据时被保守降到最低优先级。"""
        return structured_agent_result(
            result,
            binding=selection.binding if selection else None,
            provider_agent_id=agent_id,
        )

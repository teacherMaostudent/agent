"""多 Agent 委派的运行期管理边界。

Graph 只表达已发布执行计划中的状态流转。这个模块承接子 Agent 的冻结绑定
校验、预算切分与实际委派调用，避免把组织管理能力逐步塞进 LangGraph 节点。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

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

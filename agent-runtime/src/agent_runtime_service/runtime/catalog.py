"""Allow-listed Agent catalog resolved from Control Plane release snapshots."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runtime_service.runtime.harness import ExecutorAdapter


class AgentCatalog:
    """Maps published agent ids to locally registered executors.

    The catalog never imports code from a request.  Control Plane supplies the
    immutable snapshot; this class only selects an already deployed executor.
    """

    def __init__(self) -> None:
        """初始化空白名单；业务 Agent 工厂只能在进程启动部署时注册。"""
        self._factories: dict[str, Callable[[dict[str, Any]], ExecutorAdapter]] = {}

    def register(self, agent_id: str, factory: Callable[[dict[str, Any]], ExecutorAdapter]) -> None:
        """注册唯一 Agent 工厂，重复或空标识拒绝以避免快照路由歧义。"""
        key = agent_id.strip()
        if not key or key in self._factories:
            raise ValueError(f"invalid or duplicate agent catalog entry: {agent_id}")
        self._factories[key] = factory

    def build(self, agent_id: str, snapshot: dict[str, Any]) -> ExecutorAdapter:
        """为已部署 Agent 构造执行器；未部署快照不能跨集群加载任意业务代码。"""
        try:
            factory = self._factories[agent_id.strip()]
        except KeyError as exc:
            raise LookupError(
                f"agent '{agent_id}' is not deployed in this Runtime cluster"
            ) from exc
        return factory(snapshot)

    def contains(self, agent_id: str) -> bool:
        """检查本 Runtime 集群是否部署了某 Agent，供调度器选择目标集群。"""
        return agent_id.strip() in self._factories

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
        self._factories: dict[str, Callable[[dict[str, Any]], ExecutorAdapter]] = {}

    def register(self, agent_id: str, factory: Callable[[dict[str, Any]], ExecutorAdapter]) -> None:
        key = agent_id.strip()
        if not key or key in self._factories:
            raise ValueError(f"invalid or duplicate agent catalog entry: {agent_id}")
        self._factories[key] = factory

    def build(self, agent_id: str, snapshot: dict[str, Any]) -> ExecutorAdapter:
        try:
            factory = self._factories[agent_id.strip()]
        except KeyError as exc:
            raise LookupError(
                f"agent '{agent_id}' is not deployed in this Runtime cluster"
            ) from exc
        return factory(snapshot)

    def contains(self, agent_id: str) -> bool:
        return agent_id.strip() in self._factories

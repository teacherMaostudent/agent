"""The Agent Runtime execution facade.

The harness owns the common Agent lifecycle boundary.  It deliberately does
not implement business policy, call providers directly, or replace Temporal
or LangGraph.  A business Agent supplies the graph; the harness is the stable
entry point used by APIs and Temporal Activities.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from app.agent.graph import AgentGraph
from app.agent.models import AgentRunResult, AgentState
from app.runtime.models import ApprovalResume


class ExecutorAdapter:
    """Common adapter contract for LangGraph, callable and future executors."""

    def run(self, initial: AgentState, thread_id: str) -> AgentRunResult:
        """Perform run within the ExecutorAdapter ownership boundary."""
        raise NotImplementedError

    def resume(self, thread_id: str, approval: ApprovalResume, *, max_steps: int) -> AgentRunResult:
        """Perform resume within the ExecutorAdapter ownership boundary."""
        raise NotImplementedError


class GraphExecutor(ExecutorAdapter):
    def __init__(self, graph: AgentGraph) -> None:
        """Initialize GraphExecutor dependencies and local state."""
        self.graph = graph

    def run(self, initial: AgentState, thread_id: str) -> AgentRunResult:
        """Perform run within the GraphExecutor ownership boundary."""
        return self.graph.run(initial, thread_id)

    def resume(self, thread_id: str, approval: ApprovalResume, *, max_steps: int) -> AgentRunResult:
        """Perform resume within the GraphExecutor ownership boundary."""
        return self.graph.resume(thread_id, approval, max_steps=max_steps)


class CallableExecutor(ExecutorAdapter):
    """Adapter for LangChain/Deep-Agent wrappers without coupling Runtime to them."""

    def __init__(self, run: Callable[[AgentState, str], AgentRunResult], resume: Callable[..., AgentRunResult] | None = None) -> None:
        """Initialize CallableExecutor dependencies and local state."""
        self._run = run
        self._resume = resume

    def run(self, initial: AgentState, thread_id: str) -> AgentRunResult:
        """Perform run within the CallableExecutor ownership boundary."""
        return self._run(initial, thread_id)

    def resume(self, thread_id: str, approval: ApprovalResume, *, max_steps: int) -> AgentRunResult:
        """Perform resume within the CallableExecutor ownership boundary."""
        if self._resume is None:
            raise RuntimeError("executor does not support approval resume")
        return self._resume(thread_id, approval, max_steps=max_steps)


class AgentHarness:
    """Uniform execution facade for all business Agents in Agent Runtime.

    Cross-cutting concerns that are common to every Agent should be added at
    this boundary (for example lifecycle hooks, trace attributes, policy
    validation, and cancellation checks).  Domain-specific decisions remain
    in the injected graph and policies supplied by the release snapshot.
    """

    def __init__(
        self,
        graph: AgentGraph,
        *,
        registry: Mapping[str, AgentGraph | ExecutorAdapter] | None = None,
    ) -> None:
        """Initialize AgentHarness dependencies and local state."""
        self.graph = graph
        self._default_executor = GraphExecutor(graph)
        # The default graph preserves the current single-Agent behavior.  A
        # Control Plane snapshot can later select a registered graph without
        # adding routing branches to every API and Temporal Activity.
        self._registry: dict[str, Any] = {
            key: value
            for key, value in (registry or {}).items()
        }

    def register(self, agent_id: str, graph: AgentGraph | ExecutorAdapter) -> None:
        """Register a business graph during process startup.

        Registration is intentionally code-owned, not dynamically loaded from
        a request.  The Control Plane chooses an ``agent_id`` in a signed,
        immutable snapshot; the Runtime only resolves it against this
        allow-listed registry.
        """
        normalized = agent_id.strip()
        if not normalized:
            raise ValueError("agent_id must not be empty")
        if normalized in self._registry:
            raise ValueError(f"agent_id already registered: {normalized}")
        self._registry[normalized] = graph

    def register_executor(self, agent_id: str, executor: ExecutorAdapter) -> None:
        """Register a non-LangGraph executor (e.g. LangChain or Deep Agent)."""
        normalized = agent_id.strip()
        if not normalized:
            raise ValueError("agent_id must not be empty")
        if normalized in self._registry:
            raise ValueError(f"agent_id already registered: {normalized}")
        self._registry[normalized] = executor

    def register_from_catalog(self, agent_id: str, snapshot: dict[str, Any], catalog) -> None:
        """Materialize an executor from the deployed, allow-listed catalog."""
        self.register_executor(agent_id, catalog.build(agent_id, snapshot))

    @property
    def registered_agents(self) -> tuple[str, ...]:
        """Perform registered agents within the AgentHarness ownership boundary."""
        return tuple(sorted(self._registry))

    def run(self, initial: AgentState, thread_id: str) -> AgentRunResult:
        """Execute a new Agent run through the configured business graph."""
        return self._resolve(initial).run(initial, thread_id)

    def resume(
        self,
        thread_id: str,
        approval: ApprovalResume,
        *,
        max_steps: int,
        agent_id: str | None = None,
    ) -> AgentRunResult:
        """Resume a suspended run without exposing the graph to callers."""
        return self._as_executor(self._registry.get(str(agent_id or "").strip(), self._default_executor)).resume(
            thread_id, approval, max_steps=max_steps
        )

    def _resolve(self, initial: AgentState) -> ExecutorAdapter:
        """Internal helper for AgentHarness; preserve its caller-facing invariant."""
        agent_id = str(initial.get("agent_id") or "").strip()
        return self._registry.get(agent_id, self.graph)

    def _as_executor(self, value: Any) -> ExecutorAdapter:
        """Internal helper for AgentHarness; preserve its caller-facing invariant."""
        return value if isinstance(value, ExecutorAdapter) else GraphExecutor(value)

    def __getattr__(self, name: str) -> Any:
        """Keep graph-specific helpers available during the migration.

        Public execution should use ``run`` and ``resume``.  Delegation keeps
        existing integrations compatible while the harness becomes the stable
        runtime boundary.
        """
        return getattr(self.graph, name)

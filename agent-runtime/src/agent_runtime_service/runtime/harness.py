"""The Agent Runtime execution facade.

The harness owns the common Agent lifecycle boundary.  It deliberately does
not implement business policy, call providers directly, or replace Temporal
or LangGraph.  A business Agent supplies the graph; the harness is the stable
entry point used by APIs and Temporal Activities.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from agent_runtime_service.agent.graph import AgentGraph
from agent_runtime_service.agent.models import AgentRunResult, AgentState
from agent_runtime_service.runtime.models import ApprovalResume


class ExecutorAdapter:
    """Common adapter contract for LangGraph, callable and future executors."""

    def run(self, initial: AgentState, thread_id: str) -> AgentRunResult:
        """定义新运行的统一执行契约；具体执行器不得改变 Runtime 结果模型。"""
        raise NotImplementedError

    def resume(self, thread_id: str, approval: ApprovalResume, *, max_steps: int) -> AgentRunResult:
        """定义审批恢复契约；不支持恢复的执行器必须显式拒绝。"""
        raise NotImplementedError


class GraphExecutor(ExecutorAdapter):
    def __init__(self, graph: AgentGraph) -> None:
        """包装现有 LangGraph，使 Harness 不依赖其具体实现。"""
        self.graph = graph

    def run(self, initial: AgentState, thread_id: str) -> AgentRunResult:
        """委派新运行，保留图的检查点线程标识。"""
        return self.graph.run(initial, thread_id)

    def resume(self, thread_id: str, approval: ApprovalResume, *, max_steps: int) -> AgentRunResult:
        """委派已持久化图的审批恢复。"""
        return self.graph.resume(thread_id, approval, max_steps=max_steps)


class CallableExecutor(ExecutorAdapter):
    """Adapter for LangChain/Deep-Agent wrappers without coupling Runtime to them."""

    def __init__(
        self,
        run: Callable[[AgentState, str], AgentRunResult],
        resume: Callable[..., AgentRunResult] | None = None,
    ) -> None:
        """适配外部 Harness；其生命周期结果仍需符合 Runtime 契约。"""
        self._run = run
        self._resume = resume

    def run(self, initial: AgentState, thread_id: str) -> AgentRunResult:
        """调用注入的外部执行器，不在此层添加业务路由。"""
        return self._run(initial, thread_id)

    def resume(self, thread_id: str, approval: ApprovalResume, *, max_steps: int) -> AgentRunResult:
        """仅在外部执行器声明恢复能力时转发审批决定。"""
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
        """建立默认图与进程启动期白名单 Registry，保持单 Agent 向后兼容。"""
        self.graph = graph
        self._default_executor = GraphExecutor(graph)
        # The default graph preserves the current single-Agent behavior.  A
        # Control Plane snapshot can later select a registered graph without
        # adding routing branches to every API and Temporal Activity.
        self._registry: dict[str, Any] = {key: value for key, value in (registry or {}).items()}

    def register(self, agent_id: str, graph: AgentGraph | ExecutorAdapter) -> None:
        """在进程启动时注册业务图。

        注册权归代码所有，不能由请求动态加载。Control Plane 仅在已签名、不可变快照中选择
        ``agent_id``；Runtime 只会从该白名单 Registry 解析它。
        """
        normalized = agent_id.strip()
        if not normalized:
            raise ValueError("agent_id must not be empty")
        if normalized in self._registry:
            raise ValueError(f"agent_id already registered: {normalized}")
        self._registry[normalized] = graph

    def register_executor(self, agent_id: str, executor: ExecutorAdapter) -> None:
        """注册非 LangGraph 执行器；动态请求不能自行注册 Agent。"""
        normalized = agent_id.strip()
        if not normalized:
            raise ValueError("agent_id must not be empty")
        if normalized in self._registry:
            raise ValueError(f"agent_id already registered: {normalized}")
        self._registry[normalized] = executor

    def register_from_catalog(self, agent_id: str, snapshot: dict[str, Any], catalog) -> None:
        """仅从本集群部署的白名单 Catalog 实例化快照对应执行器。"""
        self.register_executor(agent_id, catalog.build(agent_id, snapshot))

    @property
    def registered_agents(self) -> tuple[str, ...]:
        """返回稳定排序的已部署 Agent 标识，用于诊断而非授权。"""
        return tuple(sorted(self._registry))

    def run(self, initial: AgentState, thread_id: str) -> AgentRunResult:
        """通过已配置的业务图执行一次新的 Agent 运行，并保持统一结果契约。"""
        return self._resolve(initial).run(initial, thread_id)

    def resume(
        self,
        thread_id: str,
        approval: ApprovalResume,
        *,
        max_steps: int,
        agent_id: str | None = None,
    ) -> AgentRunResult:
        """恢复被审批挂起的运行，对调用方隐藏具体业务图和执行器实现。"""
        return self._as_executor(
            self._registry.get(str(agent_id or "").strip(), self._default_executor)
        ).resume(thread_id, approval, max_steps=max_steps)

    def _resolve(self, initial: AgentState) -> ExecutorAdapter:
        """按状态中的发布 Agent ID 查白名单；未知 ID 回退默认图保持旧入口兼容。"""
        agent_id = str(initial.get("agent_id") or "").strip()
        return self._registry.get(agent_id, self.graph)

    def _as_executor(self, value: Any) -> ExecutorAdapter:
        """将旧图对象归一为执行器适配器，避免调用方感知框架类型。"""
        return value if isinstance(value, ExecutorAdapter) else GraphExecutor(value)

    def __getattr__(self, name: str) -> Any:
        """在迁移期保留图专属辅助能力。

        对外执行应使用 ``run`` 与 ``resume``；此委托仅保持既有集成兼容，Harness 仍是稳定运行边界。
        """
        return getattr(self.graph, name)

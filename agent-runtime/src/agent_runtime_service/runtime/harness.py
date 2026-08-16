"""Agent Runtime 的最小执行生命周期门面。

Harness 只协调已发布 Agent 的执行生命周期：发布解析、快照加载、执行上下文、
执行器选择、运行、恢复与取消。它不包含意图识别、检索、Prompt 组装、模型路由、
工具鉴权或任何领域业务逻辑。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from platform_sdk.contracts.execution import ExecutionContext
from platform_sdk.contracts.runtime_snapshot import (
    RuntimeSnapshotCompileError,
    load_runtime_snapshot_artifact,
)

from agent_runtime_service.agent.graph import AgentGraph
from agent_runtime_service.agent.models import AgentRunResult, AgentState
from agent_runtime_service.runtime.models import ApprovalResume
from agent_runtime_service.runtime.snapshot_compiler import CompiledAgentPlan, compile_snapshot


class ExecutorAdapter:
    """LangGraph、Deep Agent 等执行器必须满足的统一运行契约。"""

    def run(self, initial: AgentState, thread_id: str) -> AgentRunResult:
        """执行新运行；执行器不得改变 Runtime 的标准结果模型。"""
        raise NotImplementedError

    def resume(self, thread_id: str, approval: ApprovalResume, *, max_steps: int) -> AgentRunResult:
        """从持久化检查点恢复；不支持恢复的执行器必须显式拒绝。"""
        raise NotImplementedError


class ExecutorResolver(Protocol):
    """已部署执行器目录的只读解析契约。"""

    @property
    def profiles(self) -> tuple[str, ...]:
        """返回当前集群可执行的 Profile。"""
        ...

    def resolve(self, profile: str) -> ExecutorAdapter:
        """按发布快照 Profile 返回已部署执行器。"""
        ...


class ReleaseResolver(Protocol):
    """Control Plane 的不可变发布解析契约。"""

    def resolve(
        self,
        *,
        tenant_id: str,
        user_id: str,
        agent_id: str,
        environment: str,
        session_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        """解析允许本次运行使用的不可变发布快照。"""
        ...


class CapabilityResolver(Protocol):
    """启动期冻结能力目录向 Harness 提供的只读验证契约。"""

    def validate(self, required: list[str]) -> None:
        """确认发布计划需要的能力已部署于当前 Runtime 实例。"""
        ...

    def validate_profiles(self, required: list[str], executor_profile: str) -> None:
        """验证能力与执行器 Profile 的兼容性，避免已部署但不适用的 Provider 被选择。"""
        ...


@dataclass(frozen=True)
class LoadedSnapshot:
    """已解析且已编译的发布快照，作为 Harness 与执行器之间的边界对象。"""

    snapshot: dict[str, Any]
    snapshot_id: str
    agent_version: str
    graph_version: str
    model_policy_version: str
    plan: CompiledAgentPlan


class GraphExecutor(ExecutorAdapter):
    """将既有 LangGraph 实现适配为统一执行器。"""

    def __init__(self, graph: AgentGraph) -> None:
        """保存已装配的图；Harness 不感知图内部的节点和领域规则。"""
        self.graph = graph

    def run(self, initial: AgentState, thread_id: str) -> AgentRunResult:
        """委派新运行，并保留图自身的检查点线程标识。"""
        return self.graph.run(initial, thread_id)

    def resume(self, thread_id: str, approval: ApprovalResume, *, max_steps: int) -> AgentRunResult:
        """委派审批恢复，不在适配层解释审批语义。"""
        return self.graph.resume(thread_id, approval, max_steps=max_steps)


class SimpleExecutor(ExecutorAdapter):
    """用于纯短任务的无状态执行器，不加载 Planner、RAG、LLM 或工具。"""

    def run(self, initial: AgentState, thread_id: str) -> AgentRunResult:
        """以固定模板完成简单请求，发布者显式选择此 Profile 才会启用该受限路径。"""
        del thread_id
        return AgentRunResult(
            status="COMPLETED",
            answer=str(initial["task"]),
            steps=1,
            termination_reason="simple_executor_completed",
        )

    def resume(self, thread_id: str, approval: ApprovalResume, *, max_steps: int) -> AgentRunResult:
        """简单任务没有检查点或审批节点，恢复请求必须明确失败。"""
        del thread_id, approval, max_steps
        raise RuntimeError("simple executor does not support approval resume")


class DurableExecutor(ExecutorAdapter):
    """长期可靠 Workflow 的入口执行器，正常请求只能经 Temporal 异步队列调度。"""

    def __init__(self, worker_executor: ExecutorAdapter) -> None:
        """保存 Worker 内联执行器；该依赖不会被序列化进 Temporal 载荷。"""
        self._worker_executor = worker_executor

    def run(self, initial: AgentState, thread_id: str) -> AgentRunResult:
        """仅允许 Temporal Worker 回放已持久化提交，阻止同步 API 绕开耐久调度。"""
        if not initial.get("temporal_worker_execution"):
            raise RuntimeError("temporal-workflow executor requires the asynchronous /runs API")
        return self._worker_executor.run(initial, thread_id)

    def resume(self, thread_id: str, approval: ApprovalResume, *, max_steps: int) -> AgentRunResult:
        """仅由 Temporal Worker 调用底层检查点恢复，外部入口由 API 发送 Workflow Signal。"""
        return self._worker_executor.resume(thread_id, approval, max_steps=max_steps)


class CallableExecutor(ExecutorAdapter):
    """适配 LangChain/Deep Agent 等外部执行器，避免 Runtime 直接耦合其框架。"""

    def __init__(
        self,
        run: Callable[[AgentState, str], AgentRunResult],
        resume: Callable[..., AgentRunResult] | None = None,
    ) -> None:
        """注入外部执行函数；外部执行器仍必须遵守 Runtime 输入输出契约。"""
        self._run = run
        self._resume = resume

    def run(self, initial: AgentState, thread_id: str) -> AgentRunResult:
        """转发新运行，不在 Harness 中追加业务路由。"""
        return self._run(initial, thread_id)

    def resume(self, thread_id: str, approval: ApprovalResume, *, max_steps: int) -> AgentRunResult:
        """仅在外部执行器声明恢复能力时转发审批决定。"""
        if self._resume is None:
            raise RuntimeError("executor does not support approval resume")
        return self._resume(thread_id, approval, max_steps=max_steps)


class AgentHarness:
    """已发布 Agent 的七项生命周期协调门面。"""

    def __init__(
        self,
        *,
        release_resolver: ReleaseResolver | None,
        executor_resolver: ExecutorResolver,
        fallback_model: str,
        snapshot_required: bool,
        cancel_execution: Callable[[str, str], Any],
        capability_resolver: CapabilityResolver | None = None,
    ) -> None:
        """注入边界依赖；Harness 不创建客户端、不注册执行器、不拥有运行状态。"""
        self._release_resolver = release_resolver
        self._executor_resolver = executor_resolver
        self._fallback_model = fallback_model
        self._snapshot_required = snapshot_required
        self._cancel_execution = cancel_execution
        self._capability_resolver = capability_resolver

    @property
    def executor_profiles(self) -> tuple[str, ...]:
        """公开 Runtime 集群已部署的执行器能力，供 Control Plane 发布前验证。"""
        return self._executor_resolver.profiles

    def resolve_release(
        self,
        *,
        tenant_id: str,
        user_id: str,
        agent_id: str,
        environment: str,
        session_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        """从 Control Plane 解析发布版本；生产环境不允许无快照回退。"""
        if self._release_resolver is None:
            if self._snapshot_required:
                raise RuntimeError("control-plane release resolution is required")
            return {}
        return self._release_resolver.resolve(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            environment=environment,
            session_id=session_id,
            trace_id=trace_id,
        )

    def load_snapshot(
        self,
        resolution: dict[str, Any],
        *,
        tenant_id: str,
        agent_id: str,
    ) -> LoadedSnapshot:
        """编译已解析快照为执行计划；配置漂移在调用执行器前失败。"""
        snapshot = dict(resolution.get("snapshot") or {})
        if self._snapshot_required and not snapshot:
            raise RuntimeError("published snapshot is required for production execution")
        if self._snapshot_required:
            try:
                plan = CompiledAgentPlan.model_validate(
                    load_runtime_snapshot_artifact(
                        snapshot, tenant_id=tenant_id, agent_id=agent_id
                    ).model_dump(mode="json")
                )
            except RuntimeSnapshotCompileError as exc:
                raise RuntimeError(str(exc)) from exc
        else:
            plan = compile_snapshot(
                snapshot,
                tenant_id=tenant_id,
                agent_id=agent_id,
                fallback_model=self._fallback_model,
            )
        if self._capability_resolver is not None:
            self._capability_resolver.validate_profiles(
                plan.required_capabilities, plan.executor_profile
            )
        return LoadedSnapshot(
            snapshot=snapshot,
            snapshot_id=str(resolution.get("version_id") or "local-unversioned"),
            agent_version=str(snapshot.get("agent_version") or "local-unversioned"),
            graph_version=str(snapshot.get("graph_version") or "runtime-planner-v1"),
            model_policy_version=str(snapshot.get("model_policy_version") or "local-unversioned"),
            plan=plan,
        )

    def create_execution_context(
        self,
        *,
        request_id: str,
        trace_id: str,
        session_id: str,
        tenant_id: str,
        user_id: str,
        agent_id: str,
        loaded_snapshot: LoadedSnapshot,
        deadline_seconds: int,
        attempt_budget: int,
        run_id: str | None,
        parent_run_id: str = "",
    ) -> ExecutionContext:
        """创建不可变执行关联标识；额度策略由调用方计算，Harness 只封装上下文。"""
        return ExecutionContext.create(
            request_id=request_id,
            trace_id=trace_id,
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            agent_version=loaded_snapshot.agent_version,
            snapshot_id=loaded_snapshot.snapshot_id,
            graph_version=loaded_snapshot.graph_version,
            model_policy_version=loaded_snapshot.model_policy_version,
            deadline_seconds=deadline_seconds,
            attempt_budget=attempt_budget,
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    def resolve_executor(self, plan: CompiledAgentPlan) -> ExecutorAdapter:
        """仅根据已编译计划的 Profile 选择本集群已部署执行器。"""
        return self._executor_resolver.resolve(plan.executor_profile)

    def run(self, initial: AgentState, thread_id: str, plan: CompiledAgentPlan) -> AgentRunResult:
        """执行已准备状态；Harness 不参与意图、检索、Prompt、模型或工具决策。"""
        if not thread_id.strip():
            raise ValueError("thread_id must not be empty")
        if not initial.get("agent_id") or not initial.get("compiled_plan"):
            raise ValueError("agent_id and compiled_plan are required for execution")
        if initial.get("executor_profile") != plan.executor_profile:
            raise ValueError("execution state and compiled plan executor profiles do not match")
        return self.resolve_executor(plan).run(initial, thread_id)

    def resume(
        self,
        thread_id: str,
        approval: ApprovalResume,
        *,
        max_steps: int,
        plan: CompiledAgentPlan,
    ) -> AgentRunResult:
        """恢复已挂起运行；恢复时仍从持久化计划解析同一执行器。"""
        if not thread_id.strip():
            raise ValueError("thread_id must not be empty")
        return self.resolve_executor(plan).resume(thread_id, approval, max_steps=max_steps)

    def cancel(self, tenant_id: str, run_id: str) -> Any:
        """委派协作取消；状态写入与队列通知属于 Run Store/Queue 而非 Harness。"""
        if not tenant_id.strip() or not run_id.strip():
            raise ValueError("tenant_id and run_id must not be empty")
        return self._cancel_execution(tenant_id, run_id)

"""Temporal-backed durable Agent execution with region-aware submission.

The workflow id is tenant and run scoped, while the task queue is region
scoped.  This separation keeps retries idempotent when submission fails over
to another Temporal cluster.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from datetime import timedelta
from threading import Thread
from typing import Any
from uuid import uuid4

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import RetryPolicy
from temporalio.exceptions import TemporalError, WorkflowAlreadyStartedError

from agent_runtime_service.runtime.temporal_routing import TemporalTargetRouter

# Activities run in a separately deployed Worker process.  The process binds
# the local Runtime executor at startup rather than serialising Python callables
# into a Temporal payload.
_executor: Callable[[dict[str, Any]], dict] | None = None


def bind_runtime_executor(executor: Callable[[dict[str, Any]], dict]) -> None:
    """在 Worker 启动时绑定本地执行入口；函数本身不会跨 Temporal 序列化。"""
    global _executor
    _executor = executor


@activity.defn(name="execute_agent_run")
async def execute_agent_run(submission: dict[str, Any]) -> dict:
    """在活动线程执行同步 Runtime；未绑定执行器属于 Worker 部署错误。"""
    if _executor is None:
        raise RuntimeError("runtime activity executor is not bound")
    return await asyncio.to_thread(_executor, submission)


@activity.defn(name="resume_agent_run")
async def resume_agent_run(submission: dict[str, Any]) -> dict:
    """在持有 Runtime 依赖的 Worker 中恢复同一 Run，审批载荷不直接进入图执行器。"""
    if _executor is None:
        raise RuntimeError("runtime activity executor is not bound")
    return await asyncio.to_thread(_executor, {**submission, "operation": "resume"})


@workflow.defn(name="AgentRunWorkflow")
class AgentRunWorkflow:
    def __init__(self) -> None:
        """保存一次待消费 Inbox 控制信号；Workflow 历史耐久保存唤醒事实，Run Store 仍是真源。"""
        self._input: dict[str, Any] | None = None

    @workflow.signal
    def deliver_input(self, control_input: dict[str, Any]) -> None:
        """接收经 API 鉴权和类型校验的控制输入；同一等待点只能消费一次，防止重复恢复。"""
        if self._input is not None:
            raise ValueError("workflow input signal was already received")
        self._input = control_input

    @workflow.signal
    def approve(self, approval: dict[str, Any]) -> None:
        """兼容历史审批 Signal；新调用方统一使用 ``deliver_input``，旧 Workflow 仍可安全恢复。"""
        self.deliver_input(approval)

    @workflow.run
    async def run(self, submission: dict[str, Any]) -> dict:
        """执行并在审批暂停时等待 Signal，再从同一 LangGraph 检查点恢复。"""
        result = await workflow.execute_activity(
            "execute_agent_run",
            submission,
            start_to_close_timeout=timedelta(hours=1),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=2),
                backoff_coefficient=2,
                maximum_interval=timedelta(minutes=2),
                maximum_attempts=5,
                non_retryable_error_types=["SnapshotCompileError", "ValueError"],
            ),
        )
        while result.get("status") in {"WAITING_APPROVAL", "WAITING_INPUT"}:
            await workflow.wait_condition(lambda: self._input is not None)
            control_input, self._input = self._input, None
            result = await workflow.execute_activity(
                "resume_agent_run",
                {**submission, "control_input": control_input},
                start_to_close_timeout=timedelta(hours=1),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=2),
                    backoff_coefficient=2,
                    maximum_interval=timedelta(minutes=2),
                    maximum_attempts=5,
                    non_retryable_error_types=["ValueError"],
                ),
            )
        return result


class TemporalRunQueue:
    """Runtime queue backed by Temporal durable execution."""

    def __init__(
        self, target: str, namespace: str, task_queue: str, region_targets: str = ""
    ) -> None:
        """启动私有事件循环并连接主/区域 Temporal 集群，初始化失败应阻止接流量。"""
        self.target = target
        self.namespace = namespace
        self.task_queue = task_queue
        self.router = TemporalTargetRouter(target, region_targets)
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._clients = {target: self._call(Client.connect(target, namespace=namespace))}
        for regional_target in self.router.targets.values():
            if regional_target not in self._clients:
                self._clients[regional_target] = self._call(
                    Client.connect(regional_target, namespace=namespace)
                )

    def submit(self, submission: dict[str, Any]) -> dict[str, Any]:
        """提交幂等工作流并按数据区域故障转移。

        所有候选使用同一 workflow ID；网络超时后的重复提交会得到已存在语义，不能
        在另一区域创建第二个 Agent Run。
        """
        run_id = submission.setdefault("run_id", f"run_{uuid4().hex}")
        workflow_id = self._workflow_id(submission["tenant_id"], run_id)
        # Every candidate uses the same workflow id.  A retry after a network
        # timeout therefore resolves to WorkflowAlreadyStarted instead of
        # creating a second agent run in another region.
        region = submission.get("data_region")
        last_error: Exception | None = None
        for target in self.router.candidates(region):
            client = self._clients[target]
            try:
                self._call(
                    client.start_workflow(
                        AgentRunWorkflow.run,
                        submission,
                        id=workflow_id,
                        task_queue=self.router.task_queue_for(self.task_queue, region),
                    )
                )
                break
            except WorkflowAlreadyStartedError:
                break
            except (TemporalError, TimeoutError) as exc:
                last_error = exc
        else:
            raise RuntimeError("all Temporal region targets are unavailable") from last_error
        return {
            "run_id": run_id,
            "request_id": submission["request_id"],
            "status": "QUEUED",
            "result": {},
            "error": "",
            "cancel_requested": False,
        }

    def get(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        """跨候选集群查询运行状态；结果读取失败保留失败信息而不伪造完成。"""
        handle = None
        description = None
        for client in self._clients.values():
            candidate = client.get_workflow_handle(self._workflow_id(tenant_id, run_id))
            try:
                description = self._call(candidate.describe())
                handle = candidate
                break
            except (TemporalError, TimeoutError):
                continue
        if description is None or handle is None:
            return None
        status = _status(description.status)
        result: dict[str, Any] = {}
        error = ""
        if description.status == WorkflowExecutionStatus.COMPLETED:
            try:
                result = self._call(handle.result())
            except (TemporalError, TimeoutError) as exc:
                error = f"{type(exc).__name__}: {exc}"[:4000]
        return {
            "run_id": run_id,
            "request_id": "",
            "status": status,
            "result": result,
            "error": error,
            "cancel_requested": status == "CANCELLED",
        }

    def cancel(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        """向找到该 workflow 的集群发取消请求，再返回其当前可见状态。"""
        for client in self._clients.values():
            handle = client.get_workflow_handle(self._workflow_id(tenant_id, run_id))
            try:
                self._call(handle.cancel())
                break
            except (TemporalError, TimeoutError):
                continue
        else:
            return None
        return self.get(tenant_id, run_id)

    def resume(self, tenant_id: str, run_id: str, control_input: dict[str, Any]) -> dict[str, Any] | None:
        """向存活 Workflow 发送一次版本化 Inbox 控制信号，拒绝找不到的跨租户运行。"""
        for client in self._clients.values():
            handle = client.get_workflow_handle(self._workflow_id(tenant_id, run_id))
            try:
                self._call(handle.signal(AgentRunWorkflow.deliver_input, control_input))
                return self.get(tenant_id, run_id)
            except (TemporalError, TimeoutError):
                continue
        return None

    def close(self) -> None:
        """停止私有事件循环并有限等待后台线程，避免服务关闭无限阻塞。"""
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def _call(self, coroutine) -> Any:
        """在线程安全事件循环中等待 Temporal 协程，30 秒后把故障交由上层处理。"""
        future: Future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=30)

    @staticmethod
    def _workflow_id(tenant_id: str, run_id: str) -> str:
        """用租户和运行 ID 构造全局幂等工作流键，禁止跨租户碰撞。"""
        return f"agent-run/{tenant_id}/{run_id}"


def _status(status: WorkflowExecutionStatus) -> str:
    """将 Temporal 状态映射为稳定 Runtime API 状态，未知状态保守显示为排队。"""
    return {
        WorkflowExecutionStatus.RUNNING: "RUNNING",
        WorkflowExecutionStatus.COMPLETED: "COMPLETED",
        WorkflowExecutionStatus.FAILED: "FAILED",
        WorkflowExecutionStatus.CANCELED: "CANCELLED",
        WorkflowExecutionStatus.TERMINATED: "CANCELLED",
        WorkflowExecutionStatus.TIMED_OUT: "FAILED",
    }.get(status, "QUEUED")

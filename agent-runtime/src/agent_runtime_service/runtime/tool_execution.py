"""受控工具执行调度器。

Tool Gateway 仍是工具鉴权、审批和幂等副作用的唯一边界；本模块只在 Runtime 内决定多个
已批准调用能否并行，并用稳定 ``resource_key`` 避免相同业务资源被两个 Graph 同时修改。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Protocol, TypeVar


class ToolSchedulingMode(StrEnum):
    """Runtime 在 Tool Gateway 调用前可执行的有限调度方式。"""

    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"
    EXCLUSIVE = "exclusive"


class SideEffectBarrierOutcome(StrEnum):
    """副作用屏障的有限结论；只有允许才可进入 Tool Gateway 调用。"""

    ALLOW = "allow"
    REPLAN_REQUIRED = "replan_required"


class SideEffectBarrierRejected(RuntimeError):
    """不可安全重试或执行的副作用请求被屏障拒绝时抛出。"""


class PendingInputChecker(Protocol):
    """屏障需要的最小邮箱查询，不依赖具体数据库或 RuntimeStore。"""

    def has_pending_replan_input(self, tenant_id: str, run_id: str) -> bool:
        """返回是否有必须在副作用前处理的用户或系统输入。"""
        ...


class SideEffectBarrier:
    """在 Runtime 到 Tool Gateway 的最后边界保护不可逆操作。

    它不复制 Gateway 的权限、审批或幂等账本；只确认本次 Run 仍有效、快照已固定、
    副作用拥有稳定幂等键，并在有未处理 Steering 时让 Graph 先重规划。
    """

    def __init__(
        self,
        *,
        cancellation_checker: Callable[[str, str], bool] | None = None,
        inbox: PendingInputChecker | None = None,
    ) -> None:
        """注入只读取消与邮箱边界，屏障本身不领取消息也不变更 Run 状态。"""
        self._cancellation_checker = cancellation_checker
        self._inbox = inbox

    def before_dispatch(
        self,
        state: dict[str, object],
        policy: ToolExecutionPolicy,
        *,
        tool_execution_id: str,
    ) -> SideEffectBarrierOutcome:
        """在调用前复核关键事实；不满足时拒绝或要求回到 Context/Planner。"""
        if not policy.side_effect:
            return SideEffectBarrierOutcome.ALLOW
        tenant_id = str(state.get("tenant_id", "")).strip()
        run_id = str(state.get("run_id", "")).strip()
        snapshot_id = str(state.get("snapshot_id", "")).strip()
        if not tenant_id or not run_id or not snapshot_id:
            raise SideEffectBarrierRejected("side effect requires tenant, run, and immutable snapshot")
        if not tool_execution_id.strip():
            raise SideEffectBarrierRejected("side effect requires a deterministic idempotency key")
        if not policy.idempotent:
            raise SideEffectBarrierRejected("published side-effect tool does not declare idempotency")
        if self._cancellation_checker is not None and self._cancellation_checker(tenant_id, run_id):
            raise SideEffectBarrierRejected("run is cancelled before side-effect dispatch")
        if self._inbox is not None and self._inbox.has_pending_replan_input(tenant_id, run_id):
            return SideEffectBarrierOutcome.REPLAN_REQUIRED
        return SideEffectBarrierOutcome.ALLOW


@dataclass(frozen=True)
class ToolExecutionPolicy:
    """发布快照中可由 Runtime 使用的最小工具调度与副作用策略。

    它不替代 Tool Gateway 的 RBAC、审批、幂等账本或业务凭证；Runtime 仅据此决定
    当前 Worker 的调度方式，并把策略事实写入 Session 以支持故障恢复解释。
    """

    mode: ToolSchedulingMode
    resource_key: str
    side_effect: bool
    idempotent: bool
    approval_required: bool

    @classmethod
    def from_published_binding(
        cls, binding: dict[str, object], *, tenant_id: str, tool_name: str
    ) -> ToolExecutionPolicy:
        """从冻结工具绑定生成确定性策略，缺省值采用最保守的串行调度。"""
        risk = str(binding.get("risk", "")).lower()
        explicit = str(binding.get("execution_mode", "")).lower()
        if explicit in {item.value for item in ToolSchedulingMode}:
            mode = ToolSchedulingMode(explicit)
        elif bool(binding.get("approval_required")) or risk in {
            "high",
            "critical",
            "write_high_risk",
        }:
            mode = ToolSchedulingMode.EXCLUSIVE
        else:
            mode = ToolSchedulingMode.SEQUENTIAL
        side_effect = bool(binding.get("side_effect")) or risk not in {
            "",
            "read_only",
            "read-only",
            "low",
        }
        return cls(
            mode=mode,
            resource_key=str(binding.get("resource_key") or f"{tenant_id}:{tool_name}"),
            side_effect=side_effect,
            idempotent=bool(binding.get("idempotent", False)),
            approval_required=bool(binding.get("approval_required")),
        )

    def scheduled_call(self, *, call_id: str, tool_name: str) -> ScheduledToolCall:
        """将策略转换为调度器输入，禁止 Graph 自行构造遗漏资源键的调用。"""
        return ScheduledToolCall(
            call_id=call_id,
            tool_name=tool_name,
            mode=self.mode,
            resource_key=self.resource_key,
        )


@dataclass(frozen=True)
class ScheduledToolCall:
    """调度所需的最小工具元数据，不携带参数正文或权限凭证。"""

    call_id: str
    tool_name: str
    mode: ToolSchedulingMode
    resource_key: str


T = TypeVar("T")


class ToolExecutionEngine:
    """为同一资源提供进程内顺序与排他锁，不复制 Tool Gateway 的业务治理。"""

    def __init__(self) -> None:
        """初始化资源锁目录；锁只影响当前 Worker，跨副本幂等仍由 Tool Gateway 保证。"""
        self._catalog_lock = Lock()
        self._resource_locks: dict[str, Lock] = {}

    def execute(self, call: ScheduledToolCall, operation: Callable[[], T]) -> T:
        """按调用模式执行一个已允许调用，排他/顺序模式按稳定资源键串行化。"""
        if call.mode == ToolSchedulingMode.PARALLEL:
            return operation()
        with self._resource_lock(call.resource_key):
            return operation()

    @staticmethod
    def policy_facts(policy: ToolExecutionPolicy) -> dict[str, object]:
        """返回可审计但不含参数或凭证的副作用策略投影。"""
        return {
            "scheduling_mode": policy.mode.value,
            "resource_key": policy.resource_key,
            "side_effect": policy.side_effect,
            "idempotent": policy.idempotent,
            "approval_required": policy.approval_required,
        }

    def execute_batch(
        self, calls: Iterable[tuple[ScheduledToolCall, Callable[[], T]]]
    ) -> list[T]:
        """仅并行无冲突的只读调用；其余调用保持输入顺序，避免产生不可解释写竞争。"""
        ordered = list(calls)
        results: list[T | None] = [None] * len(ordered)
        parallel_indexes = [
            index
            for index, (call, _) in enumerate(ordered)
            if call.mode == ToolSchedulingMode.PARALLEL
        ]
        if parallel_indexes:
            with ThreadPoolExecutor(max_workers=len(parallel_indexes)) as pool:
                futures = {
                    index: pool.submit(operation)
                    for index, (_, operation) in enumerate(ordered)
                    if index in parallel_indexes
                }
                for index, future in futures.items():
                    results[index] = future.result()
        for index, (call, operation) in enumerate(ordered):
            if index not in parallel_indexes:
                results[index] = self.execute(call, operation)
        return [result for result in results]  # type: ignore[misc]

    def _resource_lock(self, resource_key: str) -> Lock:
        """按资源键取得稳定锁；空键拒绝，防止意外把所有调用当成同一资源。"""
        key = resource_key.strip()
        if not key:
            raise ValueError("scheduled tool call requires a resource key")
        with self._catalog_lock:
            return self._resource_locks.setdefault(key, Lock())

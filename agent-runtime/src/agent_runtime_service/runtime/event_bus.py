"""Runtime 进程内生命周期事件分发，不承担跨服务消息传输或审计留存。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agent_runtime_service.runtime.session_events import (
    RuntimeEventType,
    RuntimeLifecycleEvent,
)

_LOGGER = logging.getLogger(__name__)


RuntimeEventSubscriber = Callable[[RuntimeLifecycleEvent], None]


@dataclass
class RuntimeEventRegistration:
    """启动期事件投影注册句柄；关闭后不再接收事件，便于容器有序停止。"""

    name: str
    event_type: RuntimeEventType
    _bus: RuntimeEventBus
    _subscriber: RuntimeEventSubscriber
    _active: bool = True

    def close(self) -> None:
        """移除该投影订阅；只允许在容器停止或尚未冻结的启动期调用。"""
        if self._active:
            self._bus._remove(self.event_type, self._subscriber)
            self._active = False


class RuntimeHookPhase(StrEnum):
    """固定的运行时拦截阶段；生产不允许请求期添加新阶段或监听器。"""

    PRE_PROMPT = "pre_prompt"
    PRE_MODEL_REQUEST = "pre_model_request"
    POST_MODEL_RESPONSE = "post_model_response"
    PRE_TOOL_EXECUTE = "pre_tool_execute"
    POST_TOOL_RESULT = "post_tool_result"
    POST_STEP = "post_step"


class RuntimeHookRejected(RuntimeError):
    """冻结策略拒绝某次模型或工具操作时抛出，调用方不得静默绕过。"""


RuntimeHook = Callable[[dict[str, Any]], dict[str, Any] | None]


class RuntimeInterceptionPipeline:
    """按固定阶段串行运行的策略 Hook 管线。

    它吸收 DSH 的 ``pre/execute/post`` 分层思想，但不允许动态插件。每个 Hook 只能
    收紧或补充受限上下文；抛错即拒绝，确保策略基础设施故障不会放宽执行边界。
    """

    def __init__(
        self, hooks: Mapping[RuntimeHookPhase, tuple[RuntimeHook, ...]] | None = None
    ) -> None:
        """冻结启动期声明的 Hook，复制输入映射避免外部在运行中替换安全策略。"""
        self._hooks = {phase: tuple(callbacks) for phase, callbacks in (hooks or {}).items()}

    def apply(self, phase: RuntimeHookPhase, payload: dict[str, Any]) -> dict[str, Any]:
        """顺序应用同阶段 Hook；Hook 不得删除关联 ID 或越权扩张可见能力。"""
        current = dict(payload)
        protected = {
            key: current[key]
            for key in ("tenant_id", "user_id", "run_id", "trace_id", "snapshot_id")
            if key in current
        }
        for hook in self._hooks.get(phase, ()):
            try:
                update = hook(dict(current))
            except Exception as exc:
                raise RuntimeHookRejected(f"runtime hook rejected {phase.value}") from exc
            if update is not None:
                if not isinstance(update, dict):
                    raise RuntimeHookRejected(
                        f"runtime hook returned invalid payload for {phase.value}"
                    )
                current = update
            if any(current.get(key) != value for key, value in protected.items()):
                raise RuntimeHookRejected(
                    "runtime hook attempted to alter protected execution identity"
                )
        return current


class RuntimeEventBus:
    """启动期冻结订阅者的本地事件总线，订阅失败不能回滚已经完成的运行事务。"""

    def __init__(
        self,
        subscribers: Mapping[RuntimeEventType, tuple[RuntimeEventSubscriber, ...]] | None = None,
    ) -> None:
        """复制订阅映射；不提供运行时注册，避免请求路径产生不可审计的插件行为。"""
        supplied = subscribers or {}
        self._subscribers = {
            event_type: tuple(callbacks) for event_type, callbacks in supplied.items() if callbacks
        }
        self._frozen = False

    def register_projector(
        self,
        *,
        name: str,
        event_type: RuntimeEventType,
        subscriber: RuntimeEventSubscriber,
    ) -> RuntimeEventRegistration:
        """在启动期注册命名投影并返回生命周期句柄；请求期动态插件一律拒绝。"""
        if self._frozen:
            raise RuntimeError("runtime event bus is frozen after startup")
        normalized = name.strip()
        if not normalized:
            raise ValueError("runtime projector requires a name")
        callbacks = self._subscribers.setdefault(event_type, ())
        if subscriber in callbacks:
            raise ValueError(f"runtime projector is already registered: {normalized}")
        self._subscribers[event_type] = (*callbacks, subscriber)
        return RuntimeEventRegistration(normalized, event_type, self, subscriber)

    def freeze(self) -> None:
        """结束启动注册窗口；后续请求只能发布已提交事实，不能增加隐式投影。"""
        self._frozen = True

    def _remove(self, event_type: RuntimeEventType, subscriber: RuntimeEventSubscriber) -> None:
        """按身份移除一个订阅者，供注册句柄在容器关闭时有序释放。"""
        callbacks = self._subscribers.get(event_type, ())
        self._subscribers[event_type] = tuple(item for item in callbacks if item is not subscriber)

    def publish(self, event: RuntimeLifecycleEvent) -> None:
        """同步通知本进程订阅者；失败只记录日志，审计可靠性仍由 Governance Outbox 保证。"""
        for subscriber in self._subscribers.get(event.event_type, ()):
            try:
                subscriber(event)
            except Exception:
                _LOGGER.exception(
                    "runtime event subscriber failed",
                    extra={
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "run_id": event.run_id,
                        "trace_id": event.trace_id,
                    },
                )

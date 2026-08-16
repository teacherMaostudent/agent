"""Runtime 进程内生命周期事件分发，不承担跨服务消息传输或审计留存。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

from agent_runtime_service.runtime.session_events import (
    RuntimeEventType,
    RuntimeLifecycleEvent,
)

_LOGGER = logging.getLogger(__name__)


RuntimeEventSubscriber = Callable[[RuntimeLifecycleEvent], None]


class RuntimeEventBus:
    """启动期冻结订阅者的本地事件总线，订阅失败不能回滚已经完成的运行事务。"""

    def __init__(
        self,
        subscribers: Mapping[RuntimeEventType, tuple[RuntimeEventSubscriber, ...]] | None = None,
    ) -> None:
        """复制订阅映射；不提供运行时注册，避免请求路径产生不可审计的插件行为。"""
        supplied = subscribers or {}
        self._subscribers = {
            event_type: tuple(callbacks)
            for event_type, callbacks in supplied.items()
            if callbacks
        }

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

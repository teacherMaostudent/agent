from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock
from time import monotonic

from app.domain.errors import CircuitOpenError, RateLimitError


class FixedWindowRateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def acquire(self, key: str, limit_per_minute: int) -> None:
        now = monotonic()
        cutoff = now - 60
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit_per_minute:
                raise RateLimitError("tool rate limit exceeded")
            events.append(now)


@dataclass
class _CircuitState:
    consecutive_failures: int = 0
    opened_at: float | None = None


class CircuitBreaker:
    def __init__(self) -> None:
        self._states: dict[str, _CircuitState] = defaultdict(_CircuitState)
        self._lock = Lock()

    def allow(self, key: str, reset_seconds: float) -> None:
        now = monotonic()
        with self._lock:
            state = self._states[key]
            if state.opened_at is None:
                return
            if now - state.opened_at >= reset_seconds:
                state.opened_at = None
                state.consecutive_failures = 0
                return
            raise CircuitOpenError("tool circuit breaker is open")

    def record_success(self, key: str) -> None:
        with self._lock:
            self._states[key] = _CircuitState()

    def record_failure(self, key: str, threshold: int) -> None:
        with self._lock:
            state = self._states[key]
            state.consecutive_failures += 1
            if state.consecutive_failures >= threshold:
                state.opened_at = monotonic()

"""Local admission controls for tool calls.

These controls fail closed before a side effect starts.  They complement, but
do not replace, distributed rate limiting at the platform edge.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock
from time import monotonic, time

import redis

from app.domain.errors import CircuitOpenError, RateLimitError


class FixedWindowRateLimiter:
    def __init__(self) -> None:
        """Initialize FixedWindowRateLimiter dependencies and local state."""
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def acquire(self, key: str, limit_per_minute: int) -> None:
        """Reject above-limit work before any external side effect begins."""
        now = monotonic()
        cutoff = now - 60
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit_per_minute:
                raise RateLimitError("tool rate limit exceeded")
            events.append(now)


class RedisFixedWindowRateLimiter:
    _SCRIPT = """
    local current = redis.call('INCR', KEYS[1])
    if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
    if current > tonumber(ARGV[1]) then return 0 end
    return 1
    """

    def __init__(self, redis_url: str) -> None:
        """Initialize RedisFixedWindowRateLimiter dependencies and local state."""
        self._client = redis.Redis.from_url(redis_url, decode_responses=True)

    def acquire(self, key: str, limit_per_minute: int) -> None:
        """Perform acquire within the RedisFixedWindowRateLimiter ownership boundary."""
        window = int(time() // 60)
        allowed = self._client.eval(
            self._SCRIPT,
            1,
            f"tool-rate:{key}:{window}",
            limit_per_minute,
            65,
        )
        if int(allowed) != 1:
            raise RateLimitError("tool rate limit exceeded")

    def ping(self) -> bool:
        """Perform ping within the RedisFixedWindowRateLimiter ownership boundary."""
        return bool(self._client.ping())


@dataclass
class _CircuitState:
    consecutive_failures: int = 0
    opened_at: float | None = None


class CircuitBreaker:
    def __init__(self) -> None:
        """Initialize CircuitBreaker dependencies and local state."""
        self._states: dict[str, _CircuitState] = defaultdict(_CircuitState)
        self._lock = Lock()

    def allow(self, key: str, reset_seconds: float) -> None:
        """Fail fast while an unhealthy upstream circuit is open."""
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
        """Run the bounded record success operation and surface failures."""
        with self._lock:
            self._states[key] = _CircuitState()

    def record_failure(self, key: str, threshold: int) -> None:
        """Open a circuit only after the catalogued failure threshold is reached."""
        with self._lock:
            state = self._states[key]
            state.consecutive_failures += 1
            if state.consecutive_failures >= threshold:
                state.opened_at = monotonic()

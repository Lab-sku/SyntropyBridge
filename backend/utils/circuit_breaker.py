"""Circuit breaker for upstream provider calls.

Why
----
A single misbehaving upstream (slow 504s, hung connections, quota
exceeded) can drag the whole gateway down with it. The breaker wraps
an async callable and tracks consecutive failures; once the failure
threshold is reached the breaker ``opens`` for a cooldown period and
short-circuits subsequent calls with :class:`CircuitOpenError` so
the gateway can fail fast and the autoscaler / load balancer can
take over.

Design notes
------------
* Pure in-memory — no external state. Multiple workers won't share
  the breaker; that's intentional and is the same trade-off the rest
  of the platform makes (single-process FastAPI, SQLite WAL).
* Time complexity is O(1) per call. No background tasks; the breaker
  is fully lazy and recovers passively once ``cooldown_seconds`` has
  elapsed.
* Failures caused by the *caller* (e.g. ValueError from a malformed
  request) are **not** counted against the breaker — only failures
  originating from inside the wrapped coroutine are.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class CircuitOpenError(RuntimeError):
    """Raised when the breaker is open and refuses the call."""

    def __init__(self, name: str, retry_after: float) -> None:
        super().__init__(f"circuit '{name}' open; retry in {retry_after:.1f}s")
        self.name = name
        self.retry_after = retry_after


@dataclass
class _State:
    failures: int = 0
    opened_at: float = 0.0
    last_error: str = ""
    last_failure_at: float = 0.0


class CircuitBreaker:
    """A simple, async-friendly circuit breaker.

    Parameters
    ----------
    name:
        Human-readable label used in logs and in the raised error.
    failure_threshold:
        Number of consecutive failures that trip the breaker.
    cooldown_seconds:
        How long the breaker stays open before allowing a half-open
        probe call.
    expected_exceptions:
        Tuple of exception classes that *count* as failures. Anything
        else bubbles up unchanged.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
        expected_exceptions: tuple[type[BaseException], ...] = (Exception,),
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.1, float(cooldown_seconds))
        self.expected_exceptions = expected_exceptions
        self._state = _State()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def status(self) -> dict:
        s = self._state
        return {
            "name": self.name,
            "failures": s.failures,
            "open": self._is_open(),
            "opened_at": s.opened_at or None,
            "cooldown_seconds": self.cooldown_seconds,
            "last_error": s.last_error or None,
        }

    def reset(self) -> None:
        self._state = _State()

    def is_open(self) -> bool:
        """断路器是否处于 open 状态（拒绝请求）。

        包含 half-open 探测语义：冷却期已过时返回 False，允许一次探测。
        """
        return self._is_open()

    def allow_request(self) -> bool:
        """当前是否允许放过一个请求（closed 或 half-open 探测）。"""
        return not self._is_open()

    def retry_after(self) -> float:
        """距离断路器自动 half-open 还有多少秒。"""
        return self._retry_after()

    async def record_success(self) -> None:
        """记录一次成功调用。

        在 half-open 探测成功时关闭断路器；在 closed 状态下也清零失败计数，
        保证连续失败统计不会跨健康请求累积。
        """
        # 只在确有失败计数时才加锁写，避免 hot path 上的无谓锁竞争。
        if self._state.failures:
            async with self._lock:
                self._state.failures = 0
                self._state.opened_at = 0.0
                self._state.last_error = ""

    async def record_failure(self, exc: Optional[BaseException] = None) -> None:
        """记录一次失败调用。

        累加失败计数；达到阈值且尚未打开时打开断路器。``exc`` 可选，
        传入时会更新 ``last_error`` 便于排障。
        """
        async with self._lock:
            self._state.failures += 1
            if exc is not None:
                self._state.last_error = f"{type(exc).__name__}: {exc}"[:200]
            self._state.last_failure_at = time.monotonic()
            if self._state.failures >= self.failure_threshold and self._state.opened_at == 0.0:
                self._state.opened_at = time.monotonic()
                logger.warning(
                    "circuit_breaker_open name=%s failures=%s cooldown=%.1fs err=%s",
                    self.name,
                    self._state.failures,
                    self.cooldown_seconds,
                    self._state.last_error,
                )

    async def call(self, func: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        """Run ``func`` through the breaker."""
        if self._is_open():
            raise CircuitOpenError(self.name, self._retry_after())
        try:
            result = await func(*args, **kwargs)
        except self.expected_exceptions as exc:
            await self._record_failure(exc)
            raise
        # Success — close the breaker on a healthy call.
        if self._state.failures:
            async with self._lock:
                self._state.failures = 0
                self._state.opened_at = 0.0
                self._state.last_error = ""
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_open(self) -> bool:
        s = self._state
        if s.failures < self.failure_threshold:
            return False
        if s.opened_at == 0.0:
            return False
        if (time.monotonic() - s.opened_at) >= self.cooldown_seconds:
            # Half-open: allow a probe. The next failure will reopen.
            return False
        return True

    def _retry_after(self) -> float:
        s = self._state
        elapsed = time.monotonic() - s.opened_at if s.opened_at else 0.0
        return max(0.0, self.cooldown_seconds - elapsed)

    async def _record_failure(self, exc: BaseException) -> None:
        # 保留旧的私有入口供 ``call`` 内部使用，避免破坏既有调用面；
        # 实现委托给新的公共 ``record_failure``。
        await self.record_failure(exc)


# ---------------------------------------------------------------------------
# Module-level registry. Keyed by provider slug, lazily created on first
# use so unit tests can `reset()` specific breakers without disturbing
# production state.
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, CircuitBreaker] = {}
_REGISTRY_LOCK = asyncio.Lock()


async def get_breaker(name: str, **kwargs: Any) -> CircuitBreaker:
    async with _REGISTRY_LOCK:
        br = _REGISTRY.get(name)
        if br is None:
            br = CircuitBreaker(name, **kwargs)
            _REGISTRY[name] = br
        return br


def reset_all() -> None:
    for br in _REGISTRY.values():
        br.reset()


__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "get_breaker",
    "reset_all",
]

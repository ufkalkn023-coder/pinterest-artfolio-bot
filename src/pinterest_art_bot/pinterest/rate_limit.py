"""Client-side rate limiting for the Pinterest API.

Pinterest v5 enforces per-endpoint hourly quotas and answers with HTTP 429.
Reacting to 429 alone is not enough for a scheduled bot: a run that discovers
300 artworks and then reconciles and syncs boards can burn a full hour's budget
in minutes, and every rejected call becomes a failed or ambiguous publication.

This limiter throttles before the request is sent, so the budget is spent
deliberately rather than discovered through failures.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable

import structlog

logger = structlog.get_logger()


class RateLimitExceededError(RuntimeError):
    """Raised when a call would exceed a hard limit and waiting cannot help."""


class TokenBucketRateLimiter:
    """Sliding-window limiter for API calls.

    Allows ``max_calls`` within ``window_seconds``, with an optional burst
    allowance. Blocking is cooperative: callers await the limiter, so an idle
    pipeline costs nothing.

    The clock is injectable so tests never sleep.
    """

    def __init__(
        self,
        *,
        max_calls: int,
        window_seconds: float,
        burst: int = 0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_calls <= 0:
            raise ValueError("max_calls must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

        self._max_calls = max_calls
        self._window = window_seconds
        self._burst = max(0, burst)
        # Call timestamps inside the current window.
        self._calls: deque[float] = deque()
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._throttled_calls = 0

    @property
    def throttled_calls(self) -> int:
        """How many calls had to wait for budget."""
        return self._throttled_calls

    @property
    def available(self) -> int:
        """Calls still available in the current window."""
        self._evict()
        return max(0, self._max_calls + self._burst - len(self._calls))

    def _evict(self) -> None:
        """Drop timestamps that fell out of the window."""
        cutoff = self._clock() - self._window
        while self._calls and self._calls[0] <= cutoff:
            self._calls.popleft()

    def _wait_time(self) -> float:
        """Seconds until the oldest call leaves the window."""
        self._evict()
        if len(self._calls) < self._max_calls + self._burst:
            return 0.0
        oldest = self._calls[0]
        return max(0.0, oldest + self._window - self._clock())

    async def acquire(self) -> None:
        """Wait until one call fits in the budget, then consume it."""
        while True:
            async with self._lock:
                delay = self._wait_time()
                if delay <= 0:
                    self._calls.append(self._clock())
                    return
                self._throttled_calls += 1
                logger.info("pinterest_rate_limit_wait", delay_seconds=round(delay, 3))

            await self._sleep(delay)

    def try_acquire(self) -> bool:
        """Consume a call if budget allows, without waiting."""
        if self._wait_time() > 0:
            return False
        self._calls.append(self._clock())
        return True

    def reset(self) -> None:
        """Forget all recorded calls."""
        self._calls.clear()


def build_rate_limiter(
    *,
    max_calls: int,
    window_seconds: float,
    burst: int = 0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> TokenBucketRateLimiter | None:
    """Build a limiter, or None when limiting is disabled.

    A non-positive budget means "no client-side limit"; the server's own 429
    handling still applies.
    """
    if max_calls <= 0:
        logger.info("pinterest_rate_limit_disabled")
        return None
    return TokenBucketRateLimiter(
        max_calls=max_calls,
        window_seconds=window_seconds,
        burst=burst,
        clock=clock,
        sleep=sleep,
    )

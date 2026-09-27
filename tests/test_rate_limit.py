"""Tests for the Pinterest client-side rate limiter.

The clock is injected everywhere, so these tests never sleep.
"""

import asyncio

import pytest

from pinterest_art_bot.pinterest.rate_limit import (
    TokenBucketRateLimiter,
    build_rate_limiter,
)


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def fast_sleep(clock: FakeClock):
    """Sleep that advances the fake clock instead of blocking."""

    async def _sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    return _sleep


def limiter(**kwargs) -> tuple[TokenBucketRateLimiter, FakeClock]:
    base = {"max_calls": 3, "window_seconds": 60.0, "burst": 0}
    base.update(kwargs)
    clock = FakeClock()
    return (
        TokenBucketRateLimiter(clock=clock, sleep=fast_sleep(clock), **base),  # type: ignore[arg-type]
        clock,
    )


class TestBudget:
    async def test_allows_up_to_the_budget(self):
        gate, _ = limiter()
        for _ in range(3):
            assert gate.try_acquire() is True

    async def test_rejects_the_call_after_the_budget(self):
        gate, _ = limiter()
        for _ in range(3):
            gate.try_acquire()
        assert gate.try_acquire() is False

    async def test_budget_recovers_after_the_window(self):
        gate, clock = limiter()
        for _ in range(3):
            gate.try_acquire()
        assert gate.try_acquire() is False

        clock.advance(61)
        assert gate.try_acquire() is True

    async def test_burst_extends_the_budget(self):
        gate, _ = limiter(burst=2)
        for _ in range(5):
            assert gate.try_acquire() is True
        assert gate.try_acquire() is False

    async def test_available_reflects_remaining_budget(self):
        gate, _ = limiter(max_calls=2)
        assert gate.available == 2
        gate.try_acquire()
        assert gate.available == 1

    async def test_acquire_waits_exactly_until_the_window_rolls(self):
        gate, clock = limiter(max_calls=1, window_seconds=30.0)
        await gate.acquire()
        assert clock.now == 1000.0

        # The second call must wait for the window to roll over, no longer.
        await asyncio.wait_for(gate.acquire(), timeout=1)
        assert clock.now == pytest.approx(1030.0)
        assert gate.throttled_calls == 1

    async def test_waiting_never_blocks_forever(self):
        gate, _ = limiter(max_calls=1, window_seconds=5.0)
        for _ in range(4):
            await asyncio.wait_for(gate.acquire(), timeout=1)
        assert gate.available == 0

    async def test_acquire_is_immediate_within_budget(self):
        gate, _ = limiter(max_calls=5)
        for _ in range(5):
            await asyncio.wait_for(gate.acquire(), timeout=0.1)

    async def test_reset_clears_the_budget(self):
        gate, _ = limiter(max_calls=1)
        gate.try_acquire()
        assert gate.try_acquire() is False
        gate.reset()
        assert gate.try_acquire() is True

    async def test_concurrent_acquires_respect_the_budget(self):
        """Concurrent callers must not over-spend the window."""
        gate, _ = limiter(max_calls=3, window_seconds=1000.0)
        granted: list[bool] = []

        async def attempt() -> None:
            granted.append(gate.try_acquire())

        await asyncio.gather(*(attempt() for _ in range(10)))
        assert sum(granted) == 3


class TestValidation:
    def test_rejects_non_positive_budget(self):
        with pytest.raises(ValueError, match="max_calls"):
            TokenBucketRateLimiter(max_calls=0, window_seconds=60.0)

    def test_rejects_non_positive_window(self):
        with pytest.raises(ValueError, match="window_seconds"):
            TokenBucketRateLimiter(max_calls=1, window_seconds=0.0)

    def test_build_disables_limiting_when_budget_is_zero(self):
        assert build_rate_limiter(max_calls=0, window_seconds=3600.0) is None

    def test_build_creates_limiter_when_budget_is_set(self):
        assert isinstance(
            build_rate_limiter(max_calls=100, window_seconds=3600.0, burst=5),
            TokenBucketRateLimiter,
        )


class TestClientIntegration:
    async def test_http_client_throttles_before_spending_a_call(self):
        """Budget exhaustion must delay the call, not fail it."""
        import httpx

        from pinterest_art_bot.config.settings import Settings
        from pinterest_art_bot.pinterest.client import HttpPinterestClient
        from pinterest_art_bot.pinterest.rate_limit import TokenBucketRateLimiter

        clock = FakeClock()
        gate = TokenBucketRateLimiter(
            max_calls=1, window_seconds=30.0, clock=clock, sleep=fast_sleep(clock)
        )

        sent: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request.url.path)
            return httpx.Response(200, json={"items": []})

        settings = Settings(  # type: ignore[call-arg]
            _env_file=None, publish_to_pinterest=True, pinterest_access_token="tok"
        )
        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(transport=transport, base_url="https://api.test/v5")
        client = HttpPinterestClient(settings, client=http_client, limiter=gate)

        await client.list_boards()
        assert sent == ["/v5/boards"]

        # Budget is spent: the next call waits for the window instead of failing.
        await asyncio.wait_for(client.list_boards(), timeout=1)
        assert len(sent) == 2
        assert gate.throttled_calls == 1
        assert clock.now > 1000.0

    async def test_client_uses_settings_budget_by_default(self):
        import httpx

        from pinterest_art_bot.config.settings import Settings
        from pinterest_art_bot.pinterest.client import HttpPinterestClient

        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            publish_to_pinterest=True,
            pinterest_access_token="tok",
            pinterest_calls_per_hour=7,
        )
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"items": []}))
        http_client = httpx.AsyncClient(transport=transport, base_url="https://api.test/v5")
        client = HttpPinterestClient(settings, client=http_client)

        assert client._limiter is not None
        assert client._limiter.available == 7 + settings.pinterest_calls_burst

    async def test_disabled_budget_means_no_limiter(self):
        import httpx

        from pinterest_art_bot.config.settings import Settings
        from pinterest_art_bot.pinterest.client import HttpPinterestClient

        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            publish_to_pinterest=True,
            pinterest_access_token="tok",
            pinterest_calls_per_hour=0,
        )
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"items": []}))
        http_client = httpx.AsyncClient(transport=transport, base_url="https://api.test/v5")
        client = HttpPinterestClient(settings, client=http_client)
        assert client._limiter is None

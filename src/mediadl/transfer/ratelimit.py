"""Token-bucket rate limiting and a circuit breaker (spec section 7.8).

Limits exist to *comply* with providers, never to be worked around: there is
no proxy rotation, no identity switching, and no parallel-account path.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field


class TokenBucket:
    """Simple async token bucket; one per (connector, connection) or per host."""

    def __init__(self, rate_per_s: float, burst: float | None = None) -> None:
        if rate_per_s <= 0:
            raise ValueError('rate_per_s must be positive')
        self.rate = rate_per_s
        self.capacity = burst if burst is not None else max(1.0, rate_per_s)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        """Wait until ``tokens`` are available. Returns the seconds waited."""
        waited = 0.0
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                delay = deficit / self.rate
                waited += delay
                await asyncio.sleep(delay)


class RateLimiterRegistry:
    """Named buckets, created on first use."""

    def __init__(self, default_rate: float = 4.0) -> None:
        self.default_rate = default_rate
        self._buckets: dict[str, TokenBucket] = {}
        self._rates: dict[str, float] = {}

    def configure(self, key: str, rate_per_s: float, burst: float | None = None) -> None:
        self._rates[key] = rate_per_s
        self._buckets[key] = TokenBucket(rate_per_s, burst)

    def bucket(self, key: str) -> TokenBucket:
        if key not in self._buckets:
            self._buckets[key] = TokenBucket(self._rates.get(key, self.default_rate))
        return self._buckets[key]

    async def acquire(self, key: str, tokens: float = 1.0) -> float:
        return await self.bucket(key).acquire(tokens)


class CircuitOpen(Exception):
    """Raised when a connector's breaker is open."""


@dataclass
class _Window:
    outcomes: list[tuple[float, bool]] = field(default_factory=list)
    opened_at: float | None = None
    half_open: bool = False


class CircuitBreaker:
    """Per-connector breaker: >=50% failures over 20 calls in 60s opens for 60s."""

    def __init__(
        self,
        *,
        window_s: float = 60.0,
        min_calls: int = 20,
        failure_ratio: float = 0.5,
        open_s: float = 60.0,
    ) -> None:
        self.window_s = window_s
        self.min_calls = min_calls
        self.failure_ratio = failure_ratio
        self.open_s = open_s
        self._state: dict[str, _Window] = defaultdict(_Window)

    def check(self, key: str) -> None:
        window = self._state[key]
        if window.opened_at is None:
            return
        if time.monotonic() - window.opened_at >= self.open_s:
            window.half_open = True  # allow a single probe
            window.opened_at = None
            return
        raise CircuitOpen(f'circuit open for {key}')

    def record(self, key: str, *, success: bool) -> None:
        window = self._state[key]
        now = time.monotonic()
        window.outcomes.append((now, success))
        window.outcomes = [(t, ok) for t, ok in window.outcomes if now - t <= self.window_s]
        if window.half_open:
            window.half_open = False
            if not success:
                window.opened_at = now
            else:
                window.outcomes.clear()
            return
        if len(window.outcomes) >= self.min_calls:
            failures = sum(1 for _, ok in window.outcomes if not ok)
            if failures / len(window.outcomes) >= self.failure_ratio:
                window.opened_at = now
                window.outcomes.clear()

    def is_open(self, key: str) -> bool:
        try:
            self.check(key)
        except CircuitOpen:
            return True
        return False

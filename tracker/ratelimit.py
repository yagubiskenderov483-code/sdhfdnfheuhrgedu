"""Централизованный rate limiter (token bucket) и экспоненциальный backoff."""
from __future__ import annotations

import asyncio
import random
import time


class TokenBucket:
    """Глобальный лимит запросов в секунду на ВСЕ sessions вместе. rate<=0 — без лимита."""

    def __init__(self, rate: float, burst: float | None = None):
        self.rate = rate
        self.capacity = burst if burst is not None else max(1.0, rate)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    def set_rate(self, rate: float) -> None:
        self.rate = rate
        self.capacity = max(1.0, rate)

    async def acquire(self) -> None:
        if self.rate <= 0:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


def backoff_delay(attempt: int, base: float = 2.0, cap: float = 300.0) -> float:
    """attempt=0 → ~2s, 1 → ~4s, 2 → ~8s ... до cap, с джиттером."""
    d = min(cap, base * (2 ** attempt))
    return d * (0.8 + random.random() * 0.4)

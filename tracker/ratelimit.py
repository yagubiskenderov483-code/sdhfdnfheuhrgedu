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
        while True:
            # Критическая секция без await — только обновление токенов и попытка захвата.
            # Иначе под lock во время asyncio.sleep блокировались ВСЕ ждущие — глобальный rps
            # падал в 1 запрос за цикл вместо настроенного темпа.
            async with self._lock:
                if self.rate <= 0:
                    return
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                need = 1 - self.tokens
            # спим ВНЕ lock, параллельные вызовы могут обновить токены и пройти
            await asyncio.sleep(min(1.0, need / self.rate))


def backoff_delay(attempt: int, base: float = 2.0, cap: float = 300.0) -> float:
    """attempt=0 → ~2s, 1 → ~4s, 2 → ~8s ... до cap, с джиттером."""
    d = min(cap, base * (2 ** attempt))
    return d * (0.8 + random.random() * 0.4)

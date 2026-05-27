"""Tiny in-process token-bucket rate limiter.

Keyed by an arbitrary string (typically ``client_ip + path``). For
multi-process deployments this would be backed by Redis; for tests and
local development this in-memory fallback is sufficient and has the
same public contract.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(slots=True)
class _Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    """Token-bucket per-key limiter."""

    def __init__(self, *, rate_per_minute: int, burst: int | None = None) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self.rate_per_second = rate_per_minute / 60.0
        self.capacity = float(burst if burst is not None else rate_per_minute)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, *, cost: float = 1.0) -> bool:
        """Return ``True`` if the request is allowed, ``False`` otherwise."""

        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self.capacity, last_refill=now)
                self._buckets[key] = bucket
            else:
                elapsed = now - bucket.last_refill
                bucket.tokens = min(
                    self.capacity, bucket.tokens + elapsed * self.rate_per_second
                )
                bucket.last_refill = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True
            return False

    async def reset(self) -> None:
        async with self._lock:
            self._buckets.clear()


_auth_limiter: RateLimiter | None = None


def get_auth_rate_limiter() -> RateLimiter:
    """Return the shared limiter used for ``/auth/*`` endpoints."""

    global _auth_limiter
    if _auth_limiter is None:
        from app.config import get_settings

        settings = get_settings()
        _auth_limiter = RateLimiter(
            rate_per_minute=settings.rate_limit_auth_per_minute,
            burst=settings.rate_limit_auth_per_minute,
        )
    return _auth_limiter


async def reset_auth_rate_limiter() -> None:
    """Used by tests to drop accumulated state between cases."""

    global _auth_limiter
    if _auth_limiter is not None:
        await _auth_limiter.reset()
    _auth_limiter = None

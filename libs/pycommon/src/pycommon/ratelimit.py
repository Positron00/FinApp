"""Redis fixed-window rate limiter and the per-caller dependency (platform LLD §3.11; PAY-603).

No Lua (C3): one ``MULTI`` with ``INCR`` and ``EXPIRE … NX``. Subjects are hashed, so emails and
IPs never appear in Redis keys (PAY-NFR-08).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import redis.exceptions
from fastapi import Depends, Request
from redis.asyncio import Redis

from pycommon import Clock, SystemClock
from pycommon.auth import Principal, authenticate
from pycommon.errors import RateLimited, ServiceUnavailable
from pycommon.logging import get_logger, log_event
from pycommon.otel import meter
from pycommon.settings import BaseServiceSettings

__all__ = [
    "Decision",
    "FailMode",
    "Limit",
    "RateLimiter",
    "caller_rate_limit",
]

_log = get_logger("pycommon.ratelimit")
LOG_EVERY_S: Final = 10.0
CALLER_WINDOW_S: Final = 60


class FailMode(StrEnum):
    open = "open"
    closed = "closed"


@dataclass(frozen=True)
class Limit:
    scope: str
    limit: int
    window_s: int
    fail_mode: FailMode


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after_s: int
    degraded: bool


def _subject_hash(subject: str) -> str:
    """The first 32 hex characters of ``sha256(subject)``."""
    return hashlib.sha256(subject.encode()).hexdigest()[:32]


class RateLimiter:
    def __init__(
        self, redis: Redis, *, prefix: str, component: str, clock: Clock | None = None
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._component = component
        self._clock = clock or SystemClock()
        self._last_logged = float("-inf")
        m = meter()
        self._errors = m.create_counter(
            "rate_limiter_errors", unit="1", description="Redis failures in the rate limiter"
        )
        self._limited = m.create_counter(
            "rate_limited", unit="1", description="Requests denied by a rate limit"
        )
        # The series exists before any error, so alerts and dashboards see a 0 (PAY-603).
        self._errors.add(0, {"component": component})

    def _key(self, limit: Limit, subject: str, *, now_s: int) -> str:
        window = now_s // limit.window_s
        return f"{self._prefix}:{limit.scope}:{_subject_hash(subject)}:{window}"

    async def hit(self, limit: Limit, subject: str) -> Decision:
        """Count one hit; Redis failure → degraded allow (fail open) or 503 (fail closed)."""
        now_s = int(self._clock.now().timestamp())
        key = self._key(limit, subject, now_s=now_s)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, limit.window_s + 1, nx=True)
                count_raw, _ = await pipe.execute()
            count = int(count_raw)
        except (redis.exceptions.RedisError, OSError) as exc:
            self._backend_error(limit, exc)
            if limit.fail_mode is FailMode.open:
                return Decision(allowed=True, remaining=limit.limit, retry_after_s=0, degraded=True)
            raise ServiceUnavailable() from None
        if count <= limit.limit:
            return Decision(
                allowed=True, remaining=limit.limit - count, retry_after_s=0, degraded=False
            )
        window_end = (now_s // limit.window_s + 1) * limit.window_s
        self._limited.add(1, {"scope": limit.scope})
        return Decision(
            allowed=False, remaining=0, retry_after_s=max(1, window_end - now_s), degraded=False
        )

    def _backend_error(self, limit: Limit, exc: BaseException) -> None:
        self._errors.add(1, {"component": self._component})
        now = self._clock.monotonic()
        if now - self._last_logged >= LOG_EVERY_S:
            self._last_logged = now
            log_event(
                _log,
                logging.WARNING,
                "ratelimit.backend_error",
                component=self._component,
                fail_mode=str(limit.fail_mode),
                error_type=type(exc).__name__,
            )

    async def check(self, limit: Limit, subject: str) -> None:
        """Raise ``RateLimited`` (429, ``Retry-After``) or ``ServiceUnavailable`` (fail closed)."""
        decision = await self.hit(limit, subject)
        if not decision.allowed:
            raise RateLimited(retry_after_s=decision.retry_after_s)


def caller_rate_limit(
    settings: BaseServiceSettings, *, exempt_subs: Iterable[str] = ("service:web",)
) -> Callable[..., Awaitable[None]]:
    """FastAPI dependency: per ``(iss, sub)``, fail open; ``exempt_subs`` aren't counted (C17)."""
    exempt = frozenset(exempt_subs)
    user_limit = Limit("caller", settings.service_rate_limit_user, CALLER_WINDOW_S, FailMode.open)
    worker_limit = Limit(
        "caller", settings.service_rate_limit_worker, CALLER_WINDOW_S, FailMode.open
    )

    async def _caller_rate_limit(
        request: Request,
        principal: Principal = Depends(authenticate),  # noqa: B008
    ) -> None:
        if principal.sub in exempt:
            return
        limiter = getattr(getattr(request.app.state, "ctx", None), "limiter", None)
        if not isinstance(limiter, RateLimiter):
            return
        limit = worker_limit if principal.iss == "payout-worker" else user_limit
        await limiter.check(limit, principal.caller_key)

    return _caller_rate_limit

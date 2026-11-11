"""Sign-in, reset and accept limits and the account lockout (identity LLD §6.4; PAY-104, PAY-603).

Built on pycommon ``RateLimiter`` (``MULTI`` with ``INCR`` and ``EXPIRE … NX``) plus plain
``SET … NX EX`` / ``SET … EX`` / ``PTTL`` / ``GET`` / ``DEL`` for the lock. **No Lua** (C3).
Fails closed: a Redis error raises ``ServiceUnavailable`` (503) and logs
``ratelimit.backend_error``. Subjects are HMACs, so Redis holds no emails or raw IPs.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Final

import redis.exceptions
from pycommon import Clock, SystemClock
from pycommon.errors import ServiceUnavailable
from pycommon.logging import get_logger, log_event
from pycommon.ratelimit import FailMode, Limit, RateLimiter
from redis.asyncio import Redis

from identity.domain.lockout import lock_seconds
from identity.metrics import IdentityMetrics
from identity.settings import IdentitySettings

__all__ = ["PREFIX", "UNKNOWN_IP", "AuthLimits", "LockStarted", "LoginGate"]

PREFIX: Final = "identity:rl"
UNKNOWN_IP: Final = "unknown"
LOG_EVERY_S: Final = 10.0

_log = get_logger("identity.limits")
_REDIS_ERRORS: Final = (redis.exceptions.RedisError, OSError)


@dataclass(frozen=True)
class LockStarted:
    level: int
    lock_seconds: int


@dataclass(frozen=True)
class LoginGate:
    """The limiter's verdict for one login attempt.

    ``allowed`` → verify the password. Otherwise ``scope`` and ``retry_after_s`` describe the
    429, and ``lock_started`` is set when this attempt started a lock (audit ``auth.lockout``).
    """

    allowed: bool
    scope: str | None = None
    retry_after_s: int = 0
    lock_started: LockStarted | None = None


class AuthLimits:
    def __init__(
        self,
        redis: Redis,
        settings: IdentitySettings,
        metrics: IdentityMetrics,
        *,
        clock: Clock | None = None,
    ) -> None:
        s = settings
        self._redis = redis
        self._settings = s
        self._metrics = metrics
        self._clock = clock or SystemClock()
        self._last_logged = float("-inf")
        self.login = RateLimiter(redis, prefix=PREFIX, component="login", clock=self._clock)
        self.accept = RateLimiter(redis, prefix=PREFIX, component="accept", clock=self._clock)
        window = s.identity_login_window_s
        closed = FailMode.closed
        self.login_ip = Limit("login_ip", s.auth_login_limit_ip, window, closed)
        self.login_account = Limit("login_account", s.auth_login_limit_account, window, closed)
        self.accept_ip = Limit("accept_ip", s.auth_login_limit_ip, window, closed)

    @staticmethod
    def lock_key(ek: str) -> str:
        return f"{PREFIX}:login:lock:{ek}"

    @staticmethod
    def level_key(ek: str) -> str:
        return f"{PREFIX}:login:level:{ek}"

    def _backend_error(self, exc: BaseException) -> ServiceUnavailable:
        self._metrics.limiter_errors.add(1, {"component": "login"})
        now = self._clock.monotonic()
        if now - self._last_logged >= LOG_EVERY_S:
            self._last_logged = now
            log_event(
                _log,
                logging.WARNING,
                "ratelimit.backend_error",
                component="login",
                fail_mode=str(FailMode.closed),
                error_type=type(exc).__name__,
            )
        return ServiceUnavailable()

    async def check_login(self, ip_subject: str, ek: str) -> LoginGate:
        """Steps 1-3 of the login algorithm (§6.4)."""
        s = self._settings
        decision = await self.login.hit(self.login_ip, ip_subject)
        if not decision.allowed:
            return LoginGate(False, "login_ip", decision.retry_after_s)
        lock_key, level_key = self.lock_key(ek), self.level_key(ek)
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.pttl(lock_key)
                pipe.get(level_key)
                pttl_raw, level_raw = await pipe.execute()
        except _REDIS_ERRORS as exc:
            raise self._backend_error(exc) from None
        pttl = int(pttl_raw)
        if pttl != -2:  # -2: no lock; -1 can't happen (the lock always has a TTL)
            self._metrics.rate_limited.add(1, {"scope": "login_locked"})
            return LoginGate(False, "login_locked", _ceil_s(pttl))
        level = _as_int(level_raw)
        decision = await self.login.hit(self.login_account, f"{ek}:{level}")
        if decision.allowed:
            return LoginGate(True)
        nxt = level + 1
        lock_s = lock_seconds(nxt, s.identity_lockout_base_s, s.identity_lockout_max_s)
        try:
            started = await self._redis.set(lock_key, str(nxt), nx=True, ex=lock_s)
            if started:
                await self._redis.set(level_key, str(nxt), ex=s.identity_lockout_level_ttl_s)
                return LoginGate(False, "login_account", lock_s, LockStarted(nxt, lock_s))
            # A concurrent attempt locked first: report that lock, and don't audit it twice.
            other = int(await self._redis.pttl(lock_key))
        except _REDIS_ERRORS as exc:
            raise self._backend_error(exc) from None
        return LoginGate(False, "login_account", _ceil_s(other))

    async def clear_level(self, ek: str) -> None:
        """After a successful login. A failure is logged and counted only: a kept level just
        makes the next lock longer (§6.4)."""
        try:
            await self._redis.delete(self.level_key(ek))
        except _REDIS_ERRORS as exc:
            self._backend_error(exc)

    async def check_accept(self, ip_subject: str) -> int | None:
        """``accept_ip`` (R18); the ``Retry-After`` seconds when denied, else ``None``."""
        decision = await self.accept.hit(self.accept_ip, ip_subject)
        return None if decision.allowed else decision.retry_after_s


def _ceil_s(pttl_ms: int) -> int:
    return max(1, math.ceil(pttl_ms / 1000)) if pttl_ms > 0 else 1


def _as_int(raw: object) -> int:
    if isinstance(raw, bytes | str):
        try:
            return max(0, int(raw))
        except ValueError:
            return 0
    return 0

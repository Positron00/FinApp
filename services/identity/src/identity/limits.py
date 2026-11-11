"""Sign-in, reset and accept limits and the account lockout (identity LLD §6.4; PAY-104, PAY-603).

Built on pycommon ``RateLimiter`` (``MULTI`` with ``INCR`` and ``EXPIRE … NX``) plus plain
``SET … NX EX`` / ``SET … EX`` / ``PTTL`` / ``GET`` / ``DEL`` for the lock. **No Lua** (C3).
The per-IP limit counts every attempt; the per-account limit counts **failed** attempts only
(C63): the gate reads the current count, and ``record_failure`` adds one after a failed verify.
Fails closed: a Redis error raises ``ServiceUnavailable`` (503) and logs
``ratelimit.backend_error``. Subjects are HMACs, so Redis holds no emails or raw IPs.
"""

from __future__ import annotations

import hashlib
import math
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Final

import redis.exceptions
from opentelemetry.trace import Span
from pycommon import Clock, SystemClock
from pycommon.errors import ServiceUnavailable
from pycommon.otel import tracer
from pycommon.ratelimit import FailMode, Limit, RateLimiter
from redis.asyncio import Redis

from identity.domain.lockout import lock_seconds
from identity.settings import IdentitySettings

__all__ = ["PREFIX", "UNKNOWN_IP", "AuthLimits", "LockStarted", "LoginGate"]

PREFIX: Final = "identity:rl"
UNKNOWN_IP: Final = "unknown"

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
    ``account_subject`` is the level-scoped subject a failed verify is counted against.
    """

    allowed: bool
    scope: str | None = None
    retry_after_s: int = 0
    lock_started: LockStarted | None = None
    account_subject: str | None = None


class AuthLimits:
    def __init__(
        self,
        redis: Redis,
        settings: IdentitySettings,
        *,
        clock: Clock | None = None,
    ) -> None:
        s = settings
        self._redis = redis
        self._settings = s
        self._clock = clock or SystemClock()
        self.login = RateLimiter(redis, prefix=PREFIX, component="login", clock=self._clock)
        self.accept = RateLimiter(redis, prefix=PREFIX, component="accept", clock=self._clock)
        self.reset = RateLimiter(redis, prefix=PREFIX, component="reset", clock=self._clock)
        window = s.identity_login_window_s
        closed = FailMode.closed
        self.login_ip = Limit("login_ip", s.auth_login_limit_ip, window, closed)
        self.login_account = Limit("login_account", s.auth_login_limit_account, window, closed)
        self.accept_ip = Limit("accept_ip", s.auth_login_limit_ip, window, closed)
        # Reset requests are limited like login (PAY-105): per IP and per account, no lock.
        self.reset_ip = Limit("reset_ip", s.auth_login_limit_ip, window, closed)
        self.reset_account = Limit("reset_account", s.auth_login_limit_account, window, closed)

    @staticmethod
    def lock_key(ek: str) -> str:
        return f"{PREFIX}:login:lock:{ek}"

    @staticmethod
    def level_key(ek: str) -> str:
        return f"{PREFIX}:login:level:{ek}"

    def _backend_error(self, exc: BaseException) -> ServiceUnavailable:
        """Count and log (throttled, by pycommon) a lock-command Redis error; fail closed."""
        self.login.record_error(exc, fail_mode=FailMode.closed)
        return ServiceUnavailable()

    def account_counter_key(self, subject: str) -> str:
        """The key pycommon ``RateLimiter`` uses for ``login_account`` in the current window
        (``<prefix>:<scope>:<sha256(subject)[:32]>:<now // window_s>``; C17, C55)."""
        limit = self.login_account
        window = int(self._clock.now().timestamp()) // limit.window_s
        digest = hashlib.sha256(subject.encode()).hexdigest()[:32]
        return f"{PREFIX}:{limit.scope}:{digest}:{window}"

    async def check_login(self, ip_subject: str, ek: str) -> LoginGate:
        """Steps 1-3 of the login algorithm (§6.4): IP limit, lock, failed-attempt gate."""
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
            pttl = int(pttl_raw)
            if pttl != -2:  # -2: no lock; -1 can't happen (the lock always has a TTL)
                self.login.record_denied("login_locked")
                return LoginGate(False, "login_locked", _ceil_s(pttl))
            level = _as_int(level_raw)
            subject = f"{ek}:{level}"
            failures = _as_int(await self._redis.get(self.account_counter_key(subject)))
        except _REDIS_ERRORS as exc:
            raise self._backend_error(exc) from None
        if failures < self.login_account.limit:
            return LoginGate(True, account_subject=subject)
        self.login.record_denied("login_account")
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

    async def record_failure(self, gate: LoginGate) -> None:
        """Count one failed verify against ``login_account`` (fails closed: 503)."""
        if gate.account_subject is not None:
            await self.login.hit(self.login_account, gate.account_subject)

    async def clear_level(self, ek: str) -> None:
        """After a successful login. A failure is logged and counted only: a kept level just
        makes the next lock longer (§6.4)."""
        try:
            await self._redis.delete(self.level_key(ek))
        except _REDIS_ERRORS as exc:
            self._backend_error(exc)

    async def check_accept(self, ip_subject: str) -> int | None:
        """``accept_ip`` (R18); the ``Retry-After`` seconds when denied, else ``None``.

        Used by invite accept and reset confirm; traced as ``identity.ratelimit.check`` (§5.3).
        """
        with _check_span("accept") as span:
            decision = await self.accept.hit(self.accept_ip, ip_subject)
            span.set_attribute("identity.allowed", decision.allowed)
        return None if decision.allowed else decision.retry_after_s

    async def check_reset(self, ip_subject: str, ek: str) -> int | None:
        """``reset_ip`` then ``reset_account`` (§6.4); ``Retry-After`` when denied."""
        with _check_span("reset") as span:
            decision = await self.reset.hit(self.reset_ip, ip_subject)
            if decision.allowed:
                decision = await self.reset.hit(self.reset_account, ek)
            span.set_attribute("identity.allowed", decision.allowed)
        return None if decision.allowed else decision.retry_after_s


def _check_span(component: str) -> AbstractContextManager[Span]:
    """The ``identity.ratelimit.check`` span (§5.3); login opens its own in ``usecases/signin``."""
    return tracer("identity").start_as_current_span(
        "identity.ratelimit.check", attributes={"identity.component": component}
    )


def _ceil_s(pttl_ms: int) -> int:
    return max(1, math.ceil(pttl_ms / 1000)) if pttl_ms > 0 else 1


def _as_int(raw: object) -> int:
    if isinstance(raw, bytes | str):
        try:
            return max(0, int(raw))
        except ValueError:
            return 0
    return 0

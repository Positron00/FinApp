"""Login lock commands against an in-memory Redis double (identity LLD §6.4; PAY-104, PAY-603).

The integration suite runs the same code on real Redis; this one forces the rare branches: a
concurrent lock winning the ``SET NX`` race, and Redis errors in steps 2-3 (fail closed).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Self

import pytest
import redis.exceptions
from identity.limits import AuthLimits, LoginGate
from pycommon.errors import ServiceUnavailable
from pycommon.testing import FakeClock, JsonLogCapture, OtelMemory

from tests.helpers import make_settings

EK = "e" * 64


class FakePipe:
    def __init__(self, r: FakeRedis) -> None:
        self.r = r
        self.ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __getattr__(self, name: str) -> Callable[..., None]:
        def queue(*args: Any, **kw: Any) -> None:
            self.ops.append((name, args, kw))

        return queue

    async def execute(self) -> list[Any]:
        if "pipeline" in self.r.fail:
            raise redis.exceptions.ConnectionError("down")
        return [await getattr(self.r, name)(*a, **kw) for name, a, kw in self.ops]


class FakeRedis:
    """Just enough of redis.asyncio.Redis for RateLimiter and AuthLimits."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.ttl_ms: dict[str, int] = {}
        self.fail: set[str] = set()
        self.before_set_nx: Callable[[str], None] | None = None

    def pipeline(self, transaction: bool = True) -> FakePipe:
        return FakePipe(self)

    def _check(self, op: str) -> None:
        if op in self.fail:
            raise redis.exceptions.ConnectionError("down")

    async def incr(self, key: str) -> int:
        value = int(self.data.get(key, b"0")) + 1
        self.data[key] = str(value).encode()
        return value

    async def expire(self, key: str, seconds: int, nx: bool = False) -> bool:
        if nx and key in self.ttl_ms:
            return False
        self.ttl_ms[key] = seconds * 1000
        return True

    async def pttl(self, key: str) -> int:
        self._check("pttl")
        return self.ttl_ms.get(key, -2) if key in self.data else -2

    async def get(self, key: str) -> bytes | None:
        self._check("get")
        return self.data.get(key)

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        self._check("set")
        if nx and self.before_set_nx is not None:
            self.before_set_nx(key)
        if nx and key in self.data:
            return False
        self.data[key] = value.encode()
        if ex is not None:
            self.ttl_ms[key] = ex * 1000
        return True

    async def delete(self, key: str) -> int:
        self._check("delete")
        return 1 if self.data.pop(key, None) is not None else 0


def make_limits(r: FakeRedis) -> AuthLimits:
    return AuthLimits(r, make_settings(), clock=FakeClock())  # type: ignore[arg-type]


async def exhaust(limits: AuthLimits) -> None:
    """Five failed attempts (each gate allowed, then counted)."""
    for _ in range(5):
        gate = await limits.check_login("ip", EK)
        assert gate.allowed
        await limits.record_failure(gate)


async def test_only_failed_attempts_count_toward_the_account_limit() -> None:
    """PAY-104, C63: allowed attempts that aren't recorded as failures never lock; the failure
    counter is pycommon's login_account key for the level-scoped subject."""
    r = FakeRedis()
    limits = make_limits(r)
    for _ in range(10):
        gate = await limits.check_login("ip-ok", EK)
        assert gate.allowed
        assert gate.account_subject == f"{EK}:0"
    assert AuthLimits.lock_key(EK) not in r.data
    await limits.record_failure(gate)
    assert r.data[limits.account_counter_key(f"{EK}:0")] == b"1"
    await limits.record_failure(LoginGate(False, "login_ip", 5))  # nothing to count
    assert r.data[limits.account_counter_key(f"{EK}:0")] == b"1"


async def test_lock_starts_after_five_and_level_counts_restart() -> None:
    """PAY-104: the 6th attempt starts a level-1 lock of 60 s; a garbage level reads as 0."""
    r = FakeRedis()
    limits = make_limits(r)
    r.data[AuthLimits.level_key(EK)] = b"garbage"
    await exhaust(limits)
    gate = await limits.check_login("ip", EK)
    assert (gate.allowed, gate.scope, gate.retry_after_s) == (False, "login_account", 60)
    assert gate.lock_started is not None
    assert (gate.lock_started.level, gate.lock_started.lock_seconds) == (1, 60)
    assert r.data[AuthLimits.level_key(EK)] == b"1"
    locked = await limits.check_login("ip", EK)
    assert (locked.scope, locked.retry_after_s) == ("login_locked", 60)


async def test_concurrent_lock_is_reported_not_restarted() -> None:
    """PAY-104: when another attempt wins SET NX, this one is 429 with that lock's TTL and
    starts no lock of its own (so it isn't audited twice)."""
    r = FakeRedis()
    limits = make_limits(r)
    await exhaust(limits)

    def other_wins(key: str) -> None:
        r.data[key] = b"1"
        r.ttl_ms[key] = 42_500

    r.before_set_nx = other_wins
    gate = await limits.check_login("ip", EK)
    assert (gate.allowed, gate.scope, gate.retry_after_s, gate.lock_started) == (
        False,
        "login_account",
        43,
        None,
    )


@pytest.mark.parametrize("op", ["pipeline", "get", "set"])
async def test_redis_errors_fail_closed(
    op: str, otel_memory: OtelMemory, caplog_json: JsonLogCapture
) -> None:
    """PAY-603: a Redis error in the lock commands is 503, counted and logged (throttled)."""
    r = FakeRedis()
    limits = make_limits(r)
    if op == "set":
        await exhaust(limits)

    r.fail.add(op)
    for _ in range(2):
        with pytest.raises(ServiceUnavailable):
            await limits.check_login("ip", EK)
    errors = [
        v
        for a, v in otel_memory.metric_points("rate_limiter_errors")
        if a == {"component": "login"}
    ]
    assert max(errors) >= 2
    assert len(caplog_json.events("ratelimit.backend_error")) == 1


async def test_clear_level_error_is_swallowed(otel_memory: OtelMemory) -> None:
    """PAY-104: a failed DEL after a successful login is counted, and the login still succeeds."""
    r = FakeRedis()
    r.fail.add("delete")
    await make_limits(r).clear_level(EK)


async def test_lock_with_unexpected_pttl_still_blocks() -> None:
    """PAY-104: a lock key without a usable TTL still blocks, with Retry-After 1."""
    r = FakeRedis()
    r.data[AuthLimits.lock_key(EK)] = b"1"
    r.ttl_ms[AuthLimits.lock_key(EK)] = -1
    gate = await make_limits(r).check_login("ip", EK)
    assert (gate.scope, gate.retry_after_s) == ("login_locked", 1)


async def test_reset_requests_are_limited_per_ip_and_per_account() -> None:
    """PAY-105, PAY-603: reset requests are limited like login: reset_ip at
    AUTH_LOGIN_LIMIT_IP and reset_account at AUTH_LOGIN_LIMIT_ACCOUNT, with Retry-After, and no
    lock is ever started."""
    r = FakeRedis()
    settings = make_settings(auth_login_limit_ip=3, auth_login_limit_account=2)
    limits = AuthLimits(r, settings, clock=FakeClock())  # type: ignore[arg-type]
    assert await limits.check_reset("ip-1", EK) is None
    assert await limits.check_reset("ip-2", EK) is None
    retry = await limits.check_reset("ip-3", EK)  # the account's third request
    assert retry is not None
    assert 1 <= retry <= 900
    other = "f" * 64
    assert await limits.check_reset("ip-1", other) is None  # ip-1's second request
    assert await limits.check_reset("ip-1", other + "x") is None  # its third
    assert await limits.check_reset("ip-1", other + "y") is not None  # its fourth: reset_ip
    assert not any(":lock:" in k for k in r.data)

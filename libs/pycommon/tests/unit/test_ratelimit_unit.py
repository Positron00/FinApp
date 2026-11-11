"""Rate limiter with a fake Redis (platform LLD §3.11; PAY-603, PAY-NFR-08, C17, C55)."""

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
import redis.exceptions
from fastapi import Request
from pycommon.auth import Principal
from pycommon.errors import RateLimited, ServiceUnavailable
from pycommon.ratelimit import Decision, FailMode, Limit, RateLimiter, caller_rate_limit
from pycommon.testing import FakeClock, JsonLogCapture, OtelMemory
from redis.asyncio import Redis

from tests.helpers import COMPANY_ID, make_settings


class FakePipe:
    def __init__(self, owner: FakeRedis) -> None:
        self.owner = owner
        self.ops: list[tuple[Any, ...]] = []

    async def __aenter__(self) -> FakePipe:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    def incr(self, key: str) -> None:
        self.ops.append(("incr", key))

    def expire(self, key: str, ttl: int, *, nx: bool) -> None:
        self.ops.append(("expire", key, ttl, nx))

    async def execute(self) -> list[Any]:
        if self.owner.error is not None:
            raise self.owner.error
        self.owner.log.append(self.ops)
        out: list[Any] = []
        for op in self.ops:
            if op[0] == "incr":
                self.owner.counts[op[1]] = self.owner.counts.get(op[1], 0) + 1
                out.append(self.owner.counts[op[1]])
            else:
                out.append(True)
        return out


class FakeRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.log: list[list[tuple[Any, ...]]] = []
        self.error: Exception | None = None
        self.transactions: list[bool] = []

    def pipeline(self, *, transaction: bool) -> FakePipe:
        self.transactions.append(transaction)
        return FakePipe(self)


def _clock_at(ts: int) -> FakeClock:
    return FakeClock(datetime.fromtimestamp(ts, UTC))


def _limiter(fake: FakeRedis, clock: FakeClock, component: str = "login") -> RateLimiter:
    return RateLimiter(cast(Redis, fake), prefix="identity:rl", component=component, clock=clock)


async def test_key_layout_hashes_the_subject() -> None:
    """PAY-603 / NFR-08: key prefix:scope:sha256(subject)[:32]:window; MULTI INCR, EXPIRE NX."""
    fake = FakeRedis()
    limiter = _limiter(fake, _clock_at(1_000_000_030))
    limit = Limit("login_account", 5, 60, FailMode.closed)
    await limiter.hit(limit, "ada@example.com")
    digest = hashlib.sha256(b"ada@example.com").hexdigest()[:32]
    key = f"identity:rl:login_account:{digest}:{1_000_000_030 // 60}"
    assert fake.log == [[("incr", key), ("expire", key, 61, True)]]
    assert fake.transactions == [True]
    assert "ada@example.com" not in key


async def test_window_arithmetic_and_retry_after(otel_memory: OtelMemory) -> None:
    """PAY-603: allowed while count ≤ limit; then denied with retry_after = window end - now."""
    fake = FakeRedis()
    clock = _clock_at(1_000_000_040)  # 20 s into a 60 s window
    limiter = _limiter(fake, clock)
    limit = Limit("login_ip", 2, 60, FailMode.closed)
    assert await limiter.hit(limit, "1.2.3.4") == Decision(True, 1, 0, False)
    assert await limiter.hit(limit, "1.2.3.4") == Decision(True, 0, 0, False)
    denied = await limiter.hit(limit, "1.2.3.4")
    assert denied == Decision(False, 0, 40, False)
    with pytest.raises(RateLimited) as info:
        await limiter.check(limit, "1.2.3.4")
    assert info.value.headers["Retry-After"] == "40"
    clock.advance(40)  # next window
    assert (await limiter.hit(limit, "1.2.3.4")).allowed
    assert otel_memory.metric_points("rate_limited") == [({"scope": "login_ip"}, 2)]
    edge = _limiter(FakeRedis(), _clock_at(1_000_000_079))  # 59 s into the window
    one = Limit("x", 0, 60, FailMode.open)
    assert (await edge.hit(one, "s")).retry_after_s == 1


async def test_errors_counter_exists_at_zero_and_fail_modes(
    otel_memory: OtelMemory, caplog_json: JsonLogCapture, fake_clock: FakeClock
) -> None:
    """PAY-603: rate_limiter_errors_total starts at 0; open allows (degraded), closed is 503."""
    fake = FakeRedis()
    limiter = _limiter(fake, fake_clock, component="caller")
    assert otel_memory.metric_points("rate_limiter_errors") == [({"component": "caller"}, 0)]
    fake.error = redis.exceptions.TimeoutError("500 ms")
    open_limit = Limit("caller", 600, 60, FailMode.open)
    assert await limiter.hit(open_limit, "web:u") == Decision(True, 600, 0, True)
    with pytest.raises(ServiceUnavailable):
        await limiter.check(Limit("login_account", 5, 60, FailMode.closed), "a@b.c")
    fake.error = ConnectionRefusedError(61, "refused")
    await limiter.hit(open_limit, "web:u")
    assert otel_memory.metric_points("rate_limiter_errors") == [({"component": "caller"}, 3)]
    events = caplog_json.events("ratelimit.backend_error")
    assert len(events) == 1  # at most one line per 10 s
    assert (events[0]["component"], events[0]["fail_mode"]) == ("caller", "open")
    fake_clock.advance(10)
    await limiter.hit(open_limit, "web:u")
    assert len(caplog_json.events("ratelimit.backend_error")) == 2


def _principal(iss: str, sub: str) -> Principal:
    return Principal(
        iss=iss,  # type: ignore[arg-type]
        sub=sub,
        roles=frozenset(),
        cid=COMPANY_ID,
        name=None,
        sid=None,
        jti="j",
        exp=datetime.now(UTC),
    )


class SpyLimiter(RateLimiter):
    def __init__(self) -> None:
        super().__init__(cast(Redis, FakeRedis()), prefix="payroll:rl", component="caller")
        self.checked: list[tuple[Limit, str]] = []

    async def check(self, limit: Limit, subject: str) -> None:
        self.checked.append((limit, subject))


def _request(limiter: object) -> Request:
    return cast(
        Request,
        SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(ctx=SimpleNamespace(limiter=limiter)))
        ),
    )


async def test_caller_limit_selects_worker_or_user_and_exempts_service_web() -> None:
    """PAY-603 / C17: payout-worker → _WORKER limit, others → _USER; service:web exempt."""
    settings = make_settings(service_rate_limit_user=600, service_rate_limit_worker=6000)
    dep = caller_rate_limit(settings)
    spy = SpyLimiter()
    user = str(uuid4())
    await dep(_request(spy), _principal("web", user))
    await dep(_request(spy), _principal("payout-worker", "service:payout-worker"))
    await dep(_request(spy), _principal("web", "service:web"))
    assert [
        (lim.scope, lim.limit, lim.window_s, lim.fail_mode, subj) for lim, subj in spy.checked
    ] == [
        ("caller", 600, 60, FailMode.open, f"web:{user}"),
        ("caller", 6000, 60, FailMode.open, "payout-worker:service:payout-worker"),
    ]
    only_worker_exempt = caller_rate_limit(settings, exempt_subs=("service:payout-worker",))
    spy2 = SpyLimiter()
    await only_worker_exempt(_request(spy2), _principal("web", "service:web"))
    await only_worker_exempt(_request(spy2), _principal("payout-worker", "service:payout-worker"))
    assert [subj for _, subj in spy2.checked] == ["web:service:web"]
    await dep(_request(None), _principal("web", user))  # no limiter configured: no-op


async def test_caller_key_format_is_shared() -> None:
    """C17: the caller key is "{prefix}:caller:{sha256(caller_key)[:32]}:{window}" everywhere."""
    fake = FakeRedis()
    limiter = RateLimiter(
        cast(Redis, fake), prefix="payroll:rl", component="caller", clock=_clock_at(600)
    )
    dep = caller_rate_limit(make_settings())
    await dep(_request(limiter), _principal("web", "u-1"))
    digest = hashlib.sha256(b"web:u-1").hexdigest()[:32]
    assert fake.log[0][0] == ("incr", f"payroll:rl:caller:{digest}:10")


async def test_record_denied_and_record_error_for_service_decisions(
    otel_memory: OtelMemory, caplog_json: JsonLogCapture, fake_clock: FakeClock
) -> None:
    """C63: services count their own denials and Redis errors through the limiter's counters."""
    limiter = _limiter(FakeRedis(), fake_clock, component="login")
    assert limiter.component == "login"
    limiter.record_denied("login_locked")
    limiter.record_denied("login_locked")
    limiter.record_error(redis.exceptions.TimeoutError("slow"), fail_mode=FailMode.closed)
    limiter.record_error()  # within 10 s: counted, not logged again
    assert otel_memory.metric_points("rate_limited") == [({"scope": "login_locked"}, 2)]
    assert otel_memory.metric_points("rate_limiter_errors") == [({"component": "login"}, 2)]
    (event,) = caplog_json.events("ratelimit.backend_error")
    assert (event["component"], event["fail_mode"], event["error_type"]) == (
        "login",
        "closed",
        "TimeoutError",
    )

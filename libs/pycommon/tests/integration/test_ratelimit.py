"""Rate limits on real Redis (platform LLD §3.11; PAY-603)."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pycommon.app import create_app
from pycommon.errors import RateLimited, ServiceUnavailable
from pycommon.ratelimit import FailMode, Limit, RateLimiter
from pycommon.redis_client import create_redis
from pycommon.testing import FakeClock, OtelMemory, TokenFactory

from tests.helpers import COMPANY_ID
from tests.integration.conftest import RedisServer

pytestmark = pytest.mark.integration

USER = "0193a000-0000-7000-8000-0000000000aa"


def _router() -> APIRouter:
    r = APIRouter()

    @r.get("/v1/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    return r


@pytest.fixture
def two_apps(
    redis_server: RedisServer, flush_redis: None
) -> Iterator[tuple[TestClient, TestClient]]:
    tf = TokenFactory.create()
    settings = redis_server.settings(internal_jwt_public_keys=tf.keyset, service_rate_limit_user=3)

    def app() -> TestClient:
        return TestClient(create_app(settings, routers=[_router()], use_db=False, instrument=False))

    with app() as a, app() as b:
        a.headers["Authorization"] = b.headers["Authorization"] = (
            f"Bearer {tf.mint(aud='testsvc', sub=USER, cid=COMPANY_ID, ttl_s=60)}"
        )
        yield a, b


def test_two_app_instances_share_counts(two_apps: tuple[TestClient, TestClient]) -> None:
    """PAY-603: the per-caller limit is shared across replicas; past it 429 with Retry-After."""
    a, b = two_apps
    statuses = [c.get("/v1/thing").status_code for c in (a, b, a)]
    assert statuses == [200, 200, 200]
    denied = b.get("/v1/thing")
    assert denied.status_code == 429
    assert denied.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(denied.headers["retry-after"]) <= 60


async def test_limiter_window_resets(redis_server: RedisServer, flush_redis: None) -> None:
    """PAY-603: a new fixed window starts a new count; keys expire after window + 1 s."""
    clock = FakeClock(datetime(2026, 11, 10, 12, 0, 30, tzinfo=UTC))
    r = create_redis(redis_server.settings())
    try:
        limiter = RateLimiter(r, prefix="testsvc:rl", component="login", clock=clock)
        limit = Limit("login_ip", 1, 60, FailMode.closed)
        await limiter.check(limit, "10.0.0.1")
        with pytest.raises(RateLimited) as info:
            await limiter.check(limit, "10.0.0.1")
        assert info.value.retry_after_s == 30
        clock.advance(30)
        await limiter.check(limit, "10.0.0.1")
        keys = [k async for k in r.scan_iter(match="testsvc:rl:login_ip:*")]
        assert len(keys) == 2
        ttls = [await r.ttl(k) for k in keys]
        assert all(0 < t <= 61 for t in ttls)
    finally:
        await r.aclose()


async def test_redis_down_open_allows_and_closed_refuses(
    restartable_redis: RedisServer, otel_memory: OtelMemory
) -> None:
    """PAY-603: Redis stopped → fail-open allows and counts rate_limiter_errors; closed is 503."""
    r = create_redis(restartable_redis.settings())
    limiter = RateLimiter(r, prefix="testsvc:rl", component="caller")
    restartable_redis.stop()
    try:
        decision = await limiter.hit(Limit("caller", 600, 60, FailMode.open), "web:u1")
        assert decision.allowed
        assert decision.degraded
        with pytest.raises(ServiceUnavailable) as info:
            await limiter.check(Limit("login_account", 5, 60, FailMode.closed), "a@b.c")
        assert info.value.error_code == "service_unavailable"
        assert otel_memory.metric_points("rate_limiter_errors") == [({"component": "caller"}, 2)]
    finally:
        restartable_redis.start()
        await r.aclose()

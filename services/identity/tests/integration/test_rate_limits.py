"""Fail-closed limits and shared counters (identity LLD §6.4; PAY-603)."""

from __future__ import annotations

from pycommon.testing import OtelMemory

from tests.integration.conftest import HarnessFactory, free_port

PW = "correct horse battery staple"


async def test_redis_down_fails_closed_on_public_flows(
    harness: HarnessFactory, otel_memory: OtelMemory
) -> None:
    """PAY-603, PAY-105: with Redis unreachable, login, reset request, reset confirm and accept
    → 503 service_unavailable (closed), /v1/users/me still works (the caller limit fails open),
    and the limiter errors are counted."""
    async with harness(redis_port=free_port()) as h:
        uid = await h.create_user("a@example.com", password=PW)
        r = await h.login("a@example.com", PW)
        assert (r.status_code, r.json()["error"]["code"]) == (503, "service_unavailable")
        r = await h.accept("A" * 43, PW)
        assert (r.status_code, r.json()["error"]["code"]) == (503, "service_unavailable")
        r = await h.reset_request("a@example.com")
        assert (r.status_code, r.json()["error"]["code"]) == (503, "service_unavailable")
        r = await h.reset_confirm("A" * 43, PW)
        assert (r.status_code, r.json()["error"]["code"]) == (503, "service_unavailable")
        r = await h.client.get("/v1/users/me", headers=h.user_headers(uid))
        assert r.status_code == 200
    errors = {a["component"]: v for a, v in otel_memory.metric_points("rate_limiter_errors")}
    assert errors["login"] >= 1
    assert errors["accept"] >= 2
    assert errors["reset"] >= 1
    assert errors["caller"] >= 1


async def test_two_app_instances_share_login_counts(harness: HarnessFactory) -> None:
    """PAY-603: the per-IP login counter lives in Redis, so two instances share it."""
    async with harness(auth_login_limit_ip=3) as a, harness(auth_login_limit_ip=3) as b:
        for h in (a, b, a):
            assert (await h.login("x@example.com", PW, ip="192.0.2.1")).status_code == 401
        assert (await b.login("y@example.com", PW, ip="192.0.2.1")).status_code == 429

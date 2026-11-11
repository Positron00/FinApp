"""Resilient client and Guard with respx and FakeClock (platform LLD §3.10; PAY-110, PAY-404)."""

import asyncio
import logging
from collections.abc import Iterator
from datetime import timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
import respx
from pycommon import http as phttp
from pycommon.http import (
    Guard,
    ProviderPolicy,
    ProviderRateLimited,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    ResilientClient,
    parse_retry_after,
)
from pycommon.testing import FakeClock, JsonLogCapture, OtelMemory

BASE = "https://bank.test"


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


@pytest.fixture
def max_jitter(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, float]]:
    calls: list[tuple[float, float]] = []

    def uniform(lo: float, hi: float) -> float:
        calls.append((lo, hi))
        return hi

    monkeypatch.setattr(phttp._rng, "uniform", uniform)
    return calls


def _client(clock: FakeClock, **policy: Any) -> ResilientClient:
    return ResilientClient(ProviderPolicy(name="bank", base_url=BASE, **policy), clock=clock)


def test_per_attempt_timeouts(fake_clock: FakeClock) -> None:
    """Plan §9: httpx.Timeout(connect, read, write=read, pool=connect) per attempt."""
    client = _client(fake_clock, connect_timeout=1.0, read_timeout=5.0)
    t = client._client.timeout
    assert (t.connect, t.read, t.write, t.pool) == (1.0, 5.0, 5.0, 1.0)


async def test_get_retries_on_503_then_succeeds(
    mock: respx.MockRouter, fake_clock: FakeClock, max_jitter: list[tuple[float, float]]
) -> None:
    """Plan §9: idempotent GET retried on 503 with full-jitter backoff, then 2xx returned."""
    route = mock.get("/v1/transfers/k").mock(
        side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200, json={"ok": 1})]
    )
    resp = await _client(fake_clock).request("GET", "/v1/transfers/k", operation="get_transfer")
    assert resp.json() == {"ok": 1}
    assert route.call_count == 3
    assert max_jitter == [(0, 0.2), (0, 0.4)]
    assert fake_clock.sleeps == [0.2, 0.4]


async def test_full_jitter_is_capped(
    mock: respx.MockRouter, fake_clock: FakeClock, max_jitter: list[tuple[float, float]]
) -> None:
    """Plan §9: backoff is uniform(0, min(cap, base·2^n))."""
    mock.get("/x").mock(return_value=httpx.Response(504))
    client = _client(
        fake_clock, max_retries=5, backoff_base_s=0.5, backoff_cap_s=2.0, total_deadline=60
    )
    with pytest.raises(ProviderUnavailable):
        await client.request("GET", "/x", operation="x")
    assert max_jitter == [(0, 0.5), (0, 1.0), (0, 2.0), (0, 2.0), (0, 2.0)]


async def test_post_is_not_retried_without_idempotency_key(
    mock: respx.MockRouter, fake_clock: FakeClock
) -> None:
    """Plan §9: a non-idempotent request without Idempotency-Key is sent once."""
    route = mock.post("/v1/transfers").mock(return_value=httpx.Response(503))
    with pytest.raises(ProviderUnavailable) as info:
        await _client(fake_clock).request("POST", "/v1/transfers", operation="create", json={})
    assert info.value.reason == "server_error"
    assert route.call_count == 1


async def test_post_with_key_is_retried_with_the_same_key(
    mock: respx.MockRouter, fake_clock: FakeClock
) -> None:
    """Plan §9 / ADR 0003: with Idempotency-Key the POST is retried with the same key each time."""
    route = mock.post("/v1/transfers").mock(
        side_effect=[httpx.Response(503), httpx.ReadTimeout("slow"), httpx.Response(201)]
    )
    resp = await _client(fake_clock).request(
        "POST", "/v1/transfers", operation="create", json={"a": 1}, idempotency_key="p1:1"
    )
    assert resp.status_code == 201
    keys = [c.request.headers["idempotency-key"] for c in route.calls]
    assert keys == ["p1:1", "p1:1", "p1:1"]


@pytest.mark.parametrize("status", [429, 502, 503, 504])
async def test_retry_on_statuses(
    mock: respx.MockRouter, fake_clock: FakeClock, status: int
) -> None:
    """Plan §9: 429, 502, 503 and 504 are retried."""
    route = mock.get("/x").mock(side_effect=[httpx.Response(status), httpx.Response(200)])
    assert (await _client(fake_clock).request("GET", "/x", operation="x")).status_code == 200
    assert route.call_count == 2


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, ProviderRejected),
        (404, ProviderRejected),
        (409, ProviderRejected),
        (500, ProviderUnavailable),
    ],
)
async def test_other_statuses_are_not_retried(
    mock: respx.MockRouter, fake_clock: FakeClock, status: int, error: type[Exception]
) -> None:
    """Plan §9: other 4xx (and 500, not in retry_on) are not retried and map to typed errors."""
    route = mock.get("/x").mock(return_value=httpx.Response(status))
    with pytest.raises(error) as info:
        await _client(fake_clock).request("GET", "/x", operation="x")
    assert route.call_count == 1
    if isinstance(info.value, ProviderRejected):
        assert info.value.status == status


async def test_retry_after_is_honoured(mock: respx.MockRouter, fake_clock: FakeClock) -> None:
    """Plan §9: Retry-After (seconds or HTTP date) replaces the backoff when it fits."""
    when = format_datetime(fake_clock.now() + timedelta(seconds=3), usegmt=True)
    mock.get("/x").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(503, headers={"Retry-After": when}),
            httpx.Response(200),
        ]
    )
    await _client(fake_clock).request("GET", "/x", operation="x")
    assert fake_clock.sleeps[0] == 2.0
    assert fake_clock.sleeps[1] == pytest.approx(1.0, abs=0.01)  # 3 s from t0, 2 s already spent


async def test_retry_after_past_deadline_stops(
    mock: respx.MockRouter, fake_clock: FakeClock, caplog_json: JsonLogCapture
) -> None:
    """Plan §9: a Retry-After beyond the remaining deadline stops at once."""
    route = mock.get("/x").mock(return_value=httpx.Response(429, headers={"Retry-After": "30"}))
    with pytest.raises(ProviderRateLimited) as info:
        await _client(fake_clock).request("GET", "/x", operation="x")
    assert info.value.retry_after_s == 30
    assert route.call_count == 1
    assert fake_clock.sleeps == []
    (failed,) = caplog_json.events("external.request.failed")
    assert failed["reason"] == "deadline"


async def test_backoff_beyond_logical_deadline_stops(
    mock: respx.MockRouter, fake_clock: FakeClock, max_jitter: list[tuple[float, float]]
) -> None:
    """Plan §9: the deadline covers retries: no retry whose delay would pass it."""

    def slow(request: httpx.Request) -> httpx.Response:
        fake_clock.advance(7)
        return httpx.Response(503)

    route = mock.get("/x").mock(side_effect=slow)
    with pytest.raises(ProviderUnavailable):
        await _client(fake_clock, total_deadline=15, backoff_base_s=1.0).request(
            "GET", "/x", operation="x"
        )
    assert route.call_count == 2  # t=7 → wait 1 → t=15: a 2 s backoff no longer fits


async def test_total_deadline_raises_provider_timeout(
    mock: respx.MockRouter, otel_memory: OtelMemory
) -> None:
    """Plan §9: asyncio.timeout around the whole call → ProviderTimeout; counted as timeout."""

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    mock.get("/x").mock(side_effect=hang)
    client = ResilientClient(ProviderPolicy(name="bank", base_url=BASE))
    with pytest.raises(ProviderTimeout):
        await client.request("GET", "/x", operation="slow", deadline_s=0.05)
    outcomes = {a["outcome"]: v for a, v in otel_memory.metric_points("external_requests")}
    assert outcomes == {"timeout": 1}
    await client.aclose()


async def test_timeouts_and_connect_errors(mock: respx.MockRouter, fake_clock: FakeClock) -> None:
    """Plan §9: timeouts → ProviderTimeout; connect errors → ProviderUnavailable(connect_error)."""
    mock.get("/t").mock(side_effect=httpx.ReadTimeout("slow"))
    mock.get("/c").mock(side_effect=httpx.ConnectError("refused"))
    client = _client(fake_clock, max_retries=1)
    with pytest.raises(ProviderTimeout):
        await client.request("GET", "/t", operation="t")
    with pytest.raises(ProviderUnavailable) as info:
        await client.request_raw("GET", "/c", operation="c")
    assert info.value.reason == "connect_error"
    no_retry = _client(fake_clock, retry_on_timeout=False)
    route = mock.get("/t2").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(ProviderTimeout):
        await no_retry.request("GET", "/t2", operation="t")
    assert route.call_count == 1


async def test_auth_headers_awaited_per_attempt(
    mock: respx.MockRouter, fake_clock: FakeClock
) -> None:
    """Plan §9: auth_headers is awaited per attempt, so a 60 s JWT is re-minted on retries."""
    minted: list[int] = []

    async def auth() -> dict[str, str]:
        minted.append(len(minted))
        return {"Authorization": f"Bearer t{len(minted)}"}

    route = mock.get("/x").mock(side_effect=[httpx.Response(503), httpx.Response(200)])
    client = ResilientClient(
        ProviderPolicy(name="payroll", base_url=BASE), auth_headers=auth, clock=fake_clock
    )
    await client.request("GET", "/x", operation="x", headers={"X-Client": "w"})
    assert [c.request.headers["authorization"] for c in route.calls] == ["Bearer t1", "Bearer t2"]
    assert all(c.request.headers["x-client"] == "w" for c in route.calls)


async def test_request_mapping(mock: respx.MockRouter, fake_clock: FakeClock) -> None:
    """Plan §9: 2xx → response, 429 → RateLimited, 5xx → Unavailable, 4xx → Rejected."""
    mock.get("/ok").mock(return_value=httpx.Response(204))
    mock.get("/limited").mock(return_value=httpx.Response(429, headers={"Retry-After": "1"}))
    mock.get("/redirect").mock(return_value=httpx.Response(302))
    client = _client(fake_clock, max_retries=0)
    assert (await client.request("GET", "/ok", operation="ok")).status_code == 204
    with pytest.raises(ProviderRateLimited) as limited:
        await client.request("GET", "/limited", operation="l")
    assert limited.value.retry_after_s == 1.0
    with pytest.raises(ProviderRejected):
        await client.request("GET", "/redirect", operation="r")


async def test_request_raw_returns_unmapped_responses(
    mock: respx.MockRouter, fake_clock: FakeClock
) -> None:
    """PAY-404 / ADR 0003 (e): request_raw returns final 4xx/5xx responses after retries."""
    mock.post("/v1/transfers").mock(return_value=httpx.Response(422, json={"status": "rejected"}))
    route = mock.get("/v1/transfers/k").mock(return_value=httpx.Response(503))
    client = _client(fake_clock)
    resp = await client.request_raw(
        "POST", "/v1/transfers", operation="create", idempotency_key="k"
    )
    assert resp.status_code == 422
    final = await client.request_raw("GET", "/v1/transfers/k", operation="get")
    assert final.status_code == 503
    assert route.call_count == 4


async def test_metrics_spans_and_no_secrets_in_logs(
    mock: respx.MockRouter,
    fake_clock: FakeClock,
    otel_memory: OtelMemory,
    caplog_json: JsonLogCapture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan §9 / C55: per-attempt outcome labels and spans; bodies/headers/queries never logged."""
    for noisy in ("httpx", "httpcore"):  # as setup_logging configures them
        monkeypatch.setattr(logging.getLogger(noisy), "level", logging.WARNING)
    mock.get("/x").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(429),
            httpx.ReadTimeout("t"),
            httpx.Response(200),
        ]
    )
    mock.get("/bad").mock(return_value=httpx.Response(400, json={"account": "123456789"}))
    client = _client(fake_clock)
    await client.request(
        "GET",
        "/x",
        operation="get",
        params={"api_key": "q-secret"},
        headers={"X-Api-Key": "h-secret"},
    )
    with pytest.raises(ProviderRejected):
        await client.request("GET", "/bad", operation="get")
    counts = {
        (a["provider"], a["operation"], a["outcome"]): v
        for a, v in otel_memory.metric_points("external_requests")
    }
    assert counts == {
        ("bank", "get", "server_error"): 1,
        ("bank", "get", "rate_limited"): 1,
        ("bank", "get", "timeout"): 1,
        ("bank", "get", "ok"): 1,
        ("bank", "get", "client_error"): 1,
    }
    ((attrs, hist),) = otel_memory.metric_points("external_request_duration_seconds")
    assert attrs == {"provider": "bank", "operation": "get"}
    assert hist.count == 5
    spans = [s for s in otel_memory.spans() if s.name == "bank get"]
    assert [dict(s.attributes or {}).get("attempt") for s in spans] == [1, 2, 3, 4, 1]
    assert dict(spans[0].attributes or {})["http.response.status_code"] == 503
    retries = caplog_json.events("external.request.retry")
    assert [r["reason"] for r in retries] == ["503", "429", "timeout"]
    assert all({"provider", "operation", "attempt", "delay_ms"} <= set(r) for r in retries)
    for secret in ("q-secret", "h-secret", "123456789"):
        assert secret not in caplog_json.text
        assert all(secret not in str(s.attributes) for s in otel_memory.spans())


def test_parse_retry_after(fake_clock: FakeClock) -> None:
    """Plan §9: Retry-After as seconds, HTTP date, garbage, or past date."""
    now = fake_clock.now()
    assert parse_retry_after("5", now) == 5.0
    assert parse_retry_after(None, now) is None
    assert parse_retry_after("soon", now) is None
    past = format_datetime(now - timedelta(seconds=10), usegmt=True)
    assert parse_retry_after(past, now) == 0.0
    naive = (now + timedelta(seconds=4)).strftime("%a, %d %b %Y %H:%M:%S")
    assert parse_retry_after(naive, now) == pytest.approx(4.0)


async def test_guard_retries_and_maps_errors(
    fake_clock: FakeClock, max_jitter: list[tuple[float, float]]
) -> None:
    """PAY-110: Guard gives SMTP the same deadline, retries, breaker and typed errors."""
    guard = Guard(
        ProviderPolicy(name="smtp", max_retries=2, breaker_failures=100), clock=fake_clock
    )
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionResetError("reset")
        return "sent"

    assert await guard.call("send", flaky, retryable=lambda e: True) == "sent"
    assert calls["n"] == 3

    class RefusedError(Exception):
        pass

    async def refused() -> None:
        raise RefusedError("550 mailbox unavailable")

    with pytest.raises(RefusedError):
        await guard.call("send", refused, retryable=lambda e: False)

    async def timeout() -> None:
        raise TimeoutError

    with pytest.raises(ProviderTimeout):
        await guard.call("send", timeout, retryable=lambda e: True)

    async def down() -> None:
        raise OSError("unreachable")

    with pytest.raises(ProviderUnavailable) as info:
        await guard.call("send", down, retryable=lambda e: False)
    assert info.value.reason == "connect_error"

    class TransientError(Exception):
        pass

    async def transient() -> None:
        raise TransientError("451 try later")

    with pytest.raises(TransientError):
        await guard.call("send", transient, retryable=lambda e: isinstance(e, TransientError))
    assert guard.breaker.target == "smtp"


async def test_guard_deadline() -> None:
    """PAY-110: Guard's deadline cancels a hung call with ProviderTimeout."""
    guard = Guard(ProviderPolicy(name="smtp"))

    async def hang() -> None:
        await asyncio.sleep(5)

    with pytest.raises(ProviderTimeout):
        await guard.call("send", hang, retryable=lambda e: True, deadline_s=0.05)


async def test_inner_timeout_error_is_not_the_deadline() -> None:
    """Plan §9: a TimeoutError raised outside the deadline's control is not swallowed."""
    engine = phttp._Engine(ProviderPolicy(name="x"), FakeClock())

    async def attempt(n: int) -> phttp._Attempt[None]:
        raise TimeoutError("own timeout")

    with pytest.raises(TimeoutError, match="own timeout"):
        await engine.run("op", attempt, None)

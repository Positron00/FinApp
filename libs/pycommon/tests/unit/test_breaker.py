"""Circuit breaker (platform LLD §3.10; PAY-110, PAY-404, plan §9 and §11)."""

from collections.abc import Iterator

import httpx
import pytest
import respx
from pycommon.http import (
    BreakerState,
    CircuitBreaker,
    ProviderPolicy,
    ProviderRejected,
    ProviderUnavailable,
    ResilientClient,
)
from pycommon.testing import FakeClock, JsonLogCapture, OtelMemory

BASE = "https://bank.test"


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


def _state(b: CircuitBreaker) -> BreakerState:
    return b.state  # a call, so mypy doesn't narrow the property between transitions


def _breaker(clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker("bank", failures=5, reset_s=30, clock=clock)


def test_opens_after_five_consecutive_failures(fake_clock: FakeClock) -> None:
    """Plan §9: closed → open after 5 consecutive failures; a success resets the count."""
    b = _breaker(fake_clock)
    for _ in range(4):
        b.record_failure()
    b.record_success()
    for _ in range(4):
        b.record_failure()
    assert _state(b) is BreakerState.closed
    assert b.allow()
    b.record_failure()
    assert _state(b) is BreakerState.open
    assert not b.allow()
    b.record_failure()  # already open: no change
    assert _state(b) is BreakerState.open


def test_half_open_allows_exactly_one_trial(fake_clock: FakeClock) -> None:
    """Plan §9: after reset_s the breaker is half-open and lets exactly one trial through."""
    b = _breaker(fake_clock)
    for _ in range(5):
        b.record_failure()
    fake_clock.advance(29.9)
    assert not b.allow()
    fake_clock.advance(0.1)
    assert _state(b) is BreakerState.half_open
    assert b.allow()
    assert not b.allow()
    b.record_success()
    assert _state(b) is BreakerState.closed
    assert b.allow()


def test_failed_trial_reopens(fake_clock: FakeClock) -> None:
    """Plan §9: a failed half-open trial re-opens the breaker for another reset_s."""
    b = _breaker(fake_clock)
    for _ in range(5):
        b.record_failure()
    fake_clock.advance(30)
    assert b.allow()
    b.record_failure()
    assert _state(b) is BreakerState.open
    fake_clock.advance(29)
    assert not b.allow()
    fake_clock.advance(1)
    assert b.allow()


async def test_4xx_and_429_do_not_count_and_open_fails_fast(
    mock: respx.MockRouter, fake_clock: FakeClock, otel_memory: OtelMemory
) -> None:
    """Plan §9: 4xx/429 prove the target is up; open → ProviderUnavailable without sending."""
    client = ResilientClient(
        ProviderPolicy(name="bank", base_url=BASE, max_retries=0), clock=fake_clock
    )
    rejected = mock.get("/r").mock(return_value=httpx.Response(404))
    for _ in range(6):
        with pytest.raises(ProviderRejected):
            await client.request("GET", "/r", operation="r")
    limited = mock.get("/l").mock(return_value=httpx.Response(429))
    for _ in range(6):
        with pytest.raises(Exception, match="rate limited"):
            await client.request("GET", "/l", operation="l")
    assert _state(client.breaker) is BreakerState.closed
    failing = mock.get("/f").mock(return_value=httpx.Response(503))
    for _ in range(5):
        with pytest.raises(ProviderUnavailable):
            await client.request("GET", "/f", operation="f")
    assert _state(client.breaker) is BreakerState.open
    with pytest.raises(ProviderUnavailable) as info:
        await client.request("GET", "/f", operation="f")
    assert info.value.reason == "breaker_open"
    assert failing.call_count == 5
    assert (rejected.call_count, limited.call_count) == (6, 6)
    outcomes = {a["outcome"]: v for a, v in otel_memory.metric_points("external_requests")}
    assert outcomes["breaker_open"] == 1


def test_gauge_and_transition_logs(
    fake_clock: FakeClock, otel_memory: OtelMemory, caplog_json: JsonLogCapture
) -> None:
    """Plan §11: circuit_breaker_state{target}: 0 at start, 2 open, 1 half-open; transition logs."""
    b = CircuitBreaker("payroll", failures=1, reset_s=10, clock=fake_clock)

    def value() -> float:
        points = otel_memory.metric_points("circuit_breaker_state")
        return float({a["target"]: v for a, v in points}["payroll"])

    assert value() == 0
    b.record_failure()
    assert value() == 2
    fake_clock.advance(10)
    assert value() == 1
    assert b.allow()
    b.record_success()
    assert value() == 0
    events = [
        (e["event"], e["level"], e["target"])
        for e in caplog_json.records()
        if e["event"] and e["event"].startswith("external.breaker.")
    ]
    assert events == [
        ("external.breaker.opened", "warning", "payroll"),
        ("external.breaker.half_open", "info", "payroll"),
        ("external.breaker.closed", "info", "payroll"),
    ]

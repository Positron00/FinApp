"""Slow DB: 2 s of latency between the services and Postgres (testing.md §8; HLD §4 row "Slow DB";
platform LLD §3.7; PAY-605, PAY-NFR-04, C38).

Expected: a payroll request gives up at its DB budget (DB_STATEMENT_TIMEOUT_MS / 1000 +
DB_POOL_TIMEOUT_S = 4 s, cleanup included) with 503 ``db_timeout``, even on an already-pooled
connection, where every round trip is slow but no single statement is; ``/readyz`` reports the DB
check failing while the toxic is on; the service recovers as soon as the toxic is removed.
"""

from __future__ import annotations

import time

import httpx
import pytest
from _stack import METRIC_WAIT_S, Stack, Toxiproxy

LATENCY_MS = 2000
DB_BUDGET_S = 4.0  # pycommon: DB_STATEMENT_TIMEOUT_MS / 1000 + DB_POOL_TIMEOUT_S (2 s + 2 s)
BUDGET_S = DB_BUDGET_S + 1.0  # margin for the HTTP hop through Docker port forwarding
RECOVERY_S = 15.0
TIMED_OUT = (
    'http_server_request_duration_seconds_count{service_name="payroll", '
    'http_route="/v1/pay-runs", http_response_status_code="503"}'
)


def _list_runs(stack: Stack, timeout: float = 60.0) -> tuple[httpx.Response, float]:
    started = time.monotonic()
    r = httpx.get(
        f"{stack.url('PAYROLL_PORT')}/v1/pay-runs",
        params={"limit": 5},
        headers={"Authorization": f"Bearer {stack.owner_token('payroll')}"},
        timeout=timeout,
    )
    return r, time.monotonic() - started


def _code(r: httpx.Response) -> str | None:
    try:
        return str(r.json()["error"]["code"])
    except ValueError, KeyError, TypeError:
        return None


def _wait_recovered(stack: Stack) -> float:
    deadline = time.monotonic() + RECOVERY_S
    while True:
        r, _ = _list_runs(stack, timeout=5)
        if r.status_code == 200:
            return RECOVERY_S - (deadline - time.monotonic())
        assert time.monotonic() < deadline, f"payroll did not recover in {RECOVERY_S}s"
        time.sleep(0.5)


@pytest.mark.smoke
@pytest.mark.timeout(120 + METRIC_WAIT_S)
def test_slow_db_request_times_out_at_budget_and_recovers(
    stack: Stack, toxiproxy: Toxiproxy
) -> None:
    """PAY-605, HLD §4 "Slow DB": under 2 s of DB latency every payroll request ends within its
    budget with 503 db_timeout, /readyz reports the DB check failing, and payroll recovers once
    the toxic is removed (the toxiproxy fixture resets all toxics in teardown either way)."""
    baseline, _ = _list_runs(stack)
    assert baseline.status_code == 200  # the pool now holds warm connections
    timed_out = stack.prom(TIMED_OUT)

    toxiproxy.add("postgres", "slow-db", "latency", latency=LATENCY_MS, jitter=0)
    for _ in range(3):
        r, elapsed = _list_runs(stack)
        assert elapsed < BUDGET_S, f"took {elapsed:.1f}s (HTTP {r.status_code}), budget {BUDGET_S}s"
        assert r.status_code == 503
        assert _code(r) == "db_timeout"
        assert r.headers.get("x-request-id")

    ready = httpx.get(f"{stack.url('PAYROLL_PORT')}/readyz", timeout=10)
    assert ready.status_code == 503
    assert ready.json()["checks"]["db"] == "fail"

    toxiproxy.reset()
    assert _wait_recovered(stack) < RECOVERY_S
    # HLD §4: http_server_request_duration_seconds counts the three 503s
    stack.wait_increase("payroll 503s", TIMED_OUT, timed_out, 3)

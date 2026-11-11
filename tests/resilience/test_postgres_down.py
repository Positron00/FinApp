"""Postgres down (HLD §4 row "Postgres down"; plan §12.1; testing.md §8 "DB down"; PAY-605).

Toxiproxy's ``postgres`` proxy carries every runtime DB connection; disabling it closes the pooled
connections and refuses new ones. Expected: ``/readyz`` reports the DB check failing, requests
fail fast with 503 ``db_unavailable`` (well inside the 4 s DB budget), and the service recovers
without stuck pool connections once the proxy is back. Nightly.
"""

from __future__ import annotations

import time

import httpx
import pytest
from _stack import Payroll, Stack, Toxiproxy, error_code, wait_until

FAST_S = 3.0  # a refused connection must not wait out the 4 s DB budget (+ HTTP margin)
RECOVERY_S = 15.0


@pytest.mark.timeout(120)
def test_postgres_down_fails_fast_and_recovers(
    stack: Stack, toxiproxy: Toxiproxy, payroll: Payroll
) -> None:
    """PAY-605, HLD §4 "Postgres down": 503 db_unavailable fast and /readyz 503 while the DB is
    unreachable; every request succeeds again within 15 s of its return."""
    assert payroll.call("GET", "/v1/pay-runs", params={"limit": 5}).status_code == 200
    toxiproxy.set_enabled("postgres", False)

    for _ in range(3):
        started = time.monotonic()
        r = payroll.call("GET", "/v1/pay-runs", params={"limit": 5})
        elapsed = time.monotonic() - started
        assert (r.status_code, error_code(r)) == (503, "db_unavailable"), r.status_code
        assert elapsed < FAST_S, f"took {elapsed:.1f}s"
        assert r.headers.get("x-request-id")
    for port in ("PAYROLL_PORT", "IDENTITY_PORT"):
        ready = httpx.get(f"{stack.url(port)}/readyz", timeout=10)
        assert ready.status_code == 503, port
        assert ready.json()["checks"]["db"] == "fail", port

    toxiproxy.set_enabled("postgres", True)
    started = time.monotonic()
    wait_until(
        "payroll serves again",
        lambda: payroll.call("GET", "/v1/pay-runs", params={"limit": 5}).status_code == 200,
        RECOVERY_S,
        interval=0.5,
    )
    assert time.monotonic() - started < RECOVERY_S
    # no stuck connections: a burst after recovery all succeeds
    assert all(
        payroll.call("GET", "/v1/pay-runs", params={"limit": 5}).status_code == 200
        for _ in range(10)
    )
    for port in ("PAYROLL_PORT", "IDENTITY_PORT"):
        url = f"{stack.url(port)}/readyz"

        def is_ready(url: str = url) -> bool:
            return httpx.get(url, timeout=5).status_code == 200

        wait_until(f"{port} ready", is_ready, RECOVERY_S, interval=0.5)

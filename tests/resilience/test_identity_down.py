"""identity down at login (HLD §4 row "identity down"; plan §12.1, PR fault smoke; platform LLD
§7.4: web -> identity stays direct, so the test stops the container; PAY-602, PAY-605).

Expected: sign-in through the BFF gets 503 ``service_unavailable`` in the PAY-602 error format,
the BFF logs ``bff.upstream_error`` and its identity breaker opens (``circuit_breaker_state`` 2
after the web's BREAKER_FAILURES failed calls, read from the Compose config or the web's
default; _stack.web_breaker_failures); a user who is already signed in keeps working (their
session lives in Redis and payroll answers); sign-in works again once identity is back.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _stack import (
    BREAKER_FAILURES,
    METRIC_WAIT_S,
    Bff,
    Containers,
    Stack,
    error_code,
    log_events,
    wait_until,
)

RECOVERY_S = 60.0
BREAKER = 'circuit_breaker_state{service_name="web", target="identity"}'


@pytest.mark.smoke
@pytest.mark.timeout(180 + METRIC_WAIT_S)
def test_identity_down_login_503_signed_in_journeys_still_work(
    stack: Stack, containers: Containers, bff: Bff
) -> None:
    """PAY-602, HLD §4 "identity down": BFF 503 service_unavailable at sign-in; signed-in reads
    still work; recovery after identity restarts (the containers fixture restarts it anyway)."""
    t0 = datetime.now(UTC)
    cookie = bff.login()
    containers.stop("identity")

    for _ in range(BREAKER_FAILURES + 1):  # enough failed calls to open the breaker
        r = bff.login_response()
        assert (r.status_code, error_code(r)) == (503, "service_unavailable")
        assert r.json()["error"]["request_id"]
    assert log_events(stack.logs_since("web", t0), "bff.upstream_error") >= 1
    # HLD §4: circuit_breaker_state; the breaker stays open (2) until the next sign-in after
    # BREAKER_RESET_S, and nothing below calls identity
    stack.wait_prom("the BFF's identity breaker open", f"max({BREAKER})", lambda v: v == 2)

    # Signed in before the outage: the session lives in Redis and payroll answers, so the payroll
    # journeys keep working (the 60 s role re-check defers while identity is down, C63). Only
    # /api/auth/me, a forced re-check against identity, answers 503 by design.
    for path in ("/api/pay-runs", "/api/employees", "/api/settings"):
        r = bff.call(
            "GET", path, cookie=cookie, params={"limit": 5} if path != "/api/settings" else None
        )
        assert r.status_code == 200, (path, r.status_code, error_code(r))

    containers.start("identity")
    # The BFF's identity breaker half-opens after BREAKER_RESET_S (30 s), then closes.
    wait_until("sign-in works again", lambda: bff.login_response().status_code == 200, RECOVERY_S)

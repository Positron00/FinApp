"""Redis down and Redis data loss (HLD §4 rows "Redis down" and "Redis data loss / NOGROUP";
plan §12.1; testing.md §8; PAY-106, PAY-402, PAY-407, PAY-408, PAY-603, PAY-NFR-03).

- Redis down: Toxiproxy's ``redis`` proxy carries every runtime Redis connection (web, identity,
  payroll, worker). With it disabled a signed-in request gets 503 (never "no session", never
  "allow"), sign-in fails closed, ``/readyz`` fails, approve still commits and the outbox holds
  the rows; after recovery the relay catches up and the run is paid.
- FLUSHALL mid-run: with the worker paused, payroll publishes the run, then Redis loses every
  key (stream, group, sessions). The worker hits NOGROUP, recreates the group, and payroll's
  re-publish (360 s after the last publish) redrives the lost attempts: the run completes, about
  7 minutes (nightly only, C53).
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from _stack import (
    REPUBLISH_WAIT_S,
    STREAM,
    Bff,
    Containers,
    Payroll,
    Stack,
    Toxiproxy,
    assert_paid_once,
    error_code,
    log_events,
    payout_ids,
    publish_counts,
    unsent_outbox,
    wait_until,
)

RECOVERY_S = 120


def _relay_tried(stack: Stack, run_id: str) -> bool:
    rows = stack.psql(
        "SELECT count(*) FILTER (WHERE o.sent_at IS NULL AND o.relay_attempts > 0), count(*) "
        "FROM payroll.outbox o JOIN payroll.payouts p ON p.id = o.payout_id "
        f"WHERE p.pay_run_id = '{run_id}'"
    )
    failed, total = (int(x) for x in rows[0])
    return total > 0 and failed == total


@pytest.mark.timeout(RECOVERY_S + 120)
def test_redis_down_fails_closed_holds_the_outbox_and_recovers(
    stack: Stack, toxiproxy: Toxiproxy, payroll: Payroll, bff: Bff
) -> None:
    """PAY-106, PAY-402, PAY-603, HLD §4 "Redis down": session 503; the login limiter fails
    closed; payroll's /readyz fails; the service limiter fails open, so approve commits and the
    outbox keeps every row unsent; after recovery the relay sends them and the run is paid."""
    t0 = datetime.now(UTC)
    run = payroll.calculated_run()
    cookie = bff.login()
    assert bff.call("GET", "/api/pay-runs", cookie=cookie).status_code == 200

    toxiproxy.set_enabled("redis", False)
    runs = bff.call("GET", "/api/pay-runs", cookie=cookie)
    assert (runs.status_code, error_code(runs)) == (503, "service_unavailable")
    login = bff.login_response()
    assert (login.status_code, error_code(login)) == (503, "service_unavailable")
    ready = httpx.get(f"{stack.url('PAYROLL_PORT')}/readyz", timeout=10)
    assert ready.status_code == 503
    assert ready.json()["checks"]["redis"] == "fail"

    payroll.approve(run["id"])  # the service limiter fails open (PAY-603)
    ids = payout_ids(stack, run["id"])
    wait_until("the relay tried and kept every row", lambda: _relay_tried(stack, run["id"]), 30)
    assert unsent_outbox(stack, run["id"]) == len(ids)
    assert log_events(stack.logs_since("web", t0), "ratelimit.backend_error") >= 1

    toxiproxy.set_enabled("redis", True)
    payroll.wait_final(run["id"], RECOVERY_S)
    assert_paid_once(stack, payroll, run["id"])
    assert unsent_outbox(stack, run["id"]) == 0
    assert publish_counts(stack, run["id"]) == [1] * len(ids)  # the relay, not a re-publish
    # the session survived the outage
    assert bff.call("GET", "/api/pay-runs", cookie=cookie).status_code == 200


@pytest.mark.timeout(REPUBLISH_WAIT_S + 180)
def test_redis_flushall_mid_run_recreates_the_group_and_republish_completes(
    stack: Stack, containers: Containers, payroll: Payroll
) -> None:
    """PAY-407, PAY-408, PAY-NFR-03, HLD §4 "Redis data loss / NOGROUP": the published messages
    are lost (all but the one already handed to the blocked, paused reader); the worker
    recreates its group (queue.group.recreated) and payroll's re-publish redrives every
    attempt, so the run is paid once."""
    t0 = datetime.now(UTC)
    run = payroll.calculated_run()
    containers.pause("payout-worker")
    payroll.approve(run["id"])
    ids = payout_ids(stack, run["id"])
    wait_until("the relay published the run", lambda: unsent_outbox(stack, run["id"]) == 0, 30)
    assert stack.redis_int("XLEN", STREAM) >= len(ids)

    assert stack.redis("FLUSHALL") == "OK"
    assert stack.redis_int("EXISTS", STREAM) == 0
    containers.unpause("payout-worker")

    wait_until(
        "queue.group.recreated",
        lambda: log_events(stack.logs_since("payout-worker", t0), "queue.group.recreated") >= 1,
        60,
    )
    payroll.wait_final(run["id"], REPUBLISH_WAIT_S)
    assert_paid_once(stack, payroll, run["id"])
    # The paused worker was blocked in XREADGROUP, so Redis handed it the first entry at XADD
    # time (the reply waits in its socket); that one message survives the flush in the worker's
    # buffer and is processed on unpause. Every other attempt was lost and redriven by re-publish.
    redriven = [c for c in publish_counts(stack, run["id"]) if c >= 2]
    assert len(redriven) >= len(ids) - 1
    assert log_events(stack.logs_since("payroll", t0), "payout.republished") >= len(redriven)

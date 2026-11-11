"""Bank faults (HLD §4 rows "Bank down", "Bank timeout after settling", "bank-stub restart";
plan §12.1; testing.md §8 "Dependency slow or flaky"; PAY-404, PAY-407, PAY-412, PAY-NFR-02,
PAY-NFR-03).

Payout timing is production's (C53): a message with no bank answer stays pending and is reclaimed
after 60 s idle; at delivery 5 the attempt is reported ``indeterminate``; payroll re-publishes it
360 s after its last publish. The bank-down test therefore takes about 8 minutes and runs nightly
only (C53).

Each test also checks its HLD §4 row's log event and metric. "Bank timeout after settling" names
``payout.indeterminate`` / ``payroll_payouts_republished_total``: that is the branch where the
timeouts outlast the delivery budget, which the bank-down test drives (the timeout test covers the
other branch, a retry with the same key, and checks the bank-call timeouts and the replay).
"bank-stub restart" names ``bank_stub.transfer.replayed`` / ``payouts_bank_transfers_total{outcome=
"settled"}``: the test checks the bank's replay of each stored transfer and the worker's settled
count.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _stack import (
    BANK_DEADLINE_S,
    CLAIM_IDLE_S,
    DLQ_WAIT_S,
    METRIC_WAIT_S,
    REPUBLISH_WAIT_S,
    Containers,
    Payroll,
    Stack,
    Toxiproxy,
    assert_paid_once,
    disbursements,
    log_events,
    payout_ids,
    settled_count,
    transfers,
    wait_until,
)

REDELIVERY_S = BANK_DEADLINE_S + CLAIM_IDLE_S + 90
BREAKER = 'circuit_breaker_state{service_name="payout-worker", target="bank"}'
REPUBLISHED = 'payroll_payouts_republished_total{service_name="payroll"}'
SETTLED = 'payouts_bank_transfers_total{service_name="payout-worker", outcome="settled"}'
BANK_TIMEOUTS = (
    'external_requests_total{service_name="payout-worker", provider="bank", outcome="timeout"}'
)


@pytest.mark.timeout(DLQ_WAIT_S + REPUBLISH_WAIT_S + 2 * METRIC_WAIT_S + 120)
def test_bank_down_then_up_breaker_indeterminate_then_paid(
    stack: Stack, toxiproxy: Toxiproxy, payroll: Payroll
) -> None:
    """PAY-404, PAY-407, PAY-NFR-03, HLD §4 "Bank down": the breaker opens, every attempt ends
    ``indeterminate`` after the delivery budget while the run stays ``processing`` (awaiting the
    bank), nothing is paid twice, and once the bank is back the re-publish settles the run."""
    t0 = datetime.now(UTC)
    republished = stack.prom(REPUBLISHED)
    run = payroll.calculated_run()
    toxiproxy.set_enabled("bank-stub", False)
    payroll.approve(run["id"])
    ids = payout_ids(stack, run["id"])

    def all_indeterminate() -> bool:
        statuses = payroll.attempt_statuses(run["id"])
        return len(statuses) == len(ids) and set(statuses) == {"indeterminate"}

    wait_until("every attempt indeterminate", all_indeterminate, DLQ_WAIT_S, interval=5)
    assert payroll.run(run["id"])["status"] == "processing"
    assert {p["status"] for p in payroll.payouts(run["id"])} == {"requested"}
    assert settled_count(stack, ids) == 0
    worker_logs = stack.logs_since("payout-worker", t0)
    assert log_events(worker_logs, "external.breaker.opened") >= 1
    # HLD §4 "Bank down": circuit_breaker_state reached open (2) during the outage
    window = int((datetime.now(UTC) - t0).total_seconds()) + 60
    stack.wait_prom(
        "the bank breaker open", f"max(max_over_time({BREAKER}[{window}s]))", lambda v: v == 2
    )
    # HLD §4 "Bank timeout after settling", the indeterminate branch: `payout.indeterminate`
    assert log_events(worker_logs, "payout.indeterminate") >= len(ids)
    assert log_events(stack.logs_since("payroll", t0), "payout.indeterminate") >= len(ids)

    toxiproxy.set_enabled("bank-stub", True)
    payroll.wait_final(run["id"], REPUBLISH_WAIT_S)
    assert_paid_once(stack, payroll, run["id"])
    payroll_logs = stack.logs_since("payroll", t0)
    assert log_events(payroll_logs, "payout.republished") >= len(ids)
    # ... and payroll_payouts_republished_total
    stack.wait_increase("re-published payouts", REPUBLISHED, republished, len(ids))


@pytest.mark.timeout(REDELIVERY_S + METRIC_WAIT_S + 120)
def test_bank_timeout_after_settling_retries_the_same_key(
    stack: Stack, toxiproxy: Toxiproxy, payroll: Payroll
) -> None:
    """PAY-404, PAY-407, PAY-NFR-02, HLD §4 "Bank timeout after settling" (Toxiproxy ``timeout``
    on the response path): the bank settles, the worker times out without an answer and leaves
    the message pending; the next delivery sends the same key and the bank returns the original
    transfer, so each payout is paid with exactly one settled transfer."""
    t0 = datetime.now(UTC)
    timeouts = stack.prom(BANK_TIMEOUTS)
    run = payroll.calculated_run()
    toxiproxy.add("bank-stub", "lose-answers", "timeout", stream="downstream", timeout=0)
    payroll.approve(run["id"])
    ids = payout_ids(stack, run["id"])
    wait_until("the bank settles every transfer", lambda: settled_count(stack, ids) == len(ids), 60)
    before = transfers(stack, ids)

    def timed_out() -> bool:  # each delivery gave up on the bank: Retry("bank", ...) logged
        return log_events(stack.logs_since("payout-worker", t0), "queue.message.failed") >= len(ids)

    wait_until("the worker's bank timeouts", timed_out, BANK_DEADLINE_S + 30)
    assert payroll.run(run["id"])["status"] == "processing"
    stack.wait_increase("bank calls timed out", BANK_TIMEOUTS, timeouts, 1)
    toxiproxy.remove("bank-stub", "lose-answers")

    payroll.wait_final(run["id"], REDELIVERY_S)
    assert_paid_once(stack, payroll, run["id"])
    assert transfers(stack, ids) == before
    for p in payroll.payouts(run["id"]):
        assert [a["attempt"] for a in p["attempts"]] == [1]
    # the retry sent the same key and the bank answered with the stored transfer
    assert log_events(stack.logs_since("bank-stub", t0), "bank_stub.transfer.replayed") >= len(ids)


@pytest.mark.timeout(REDELIVERY_S + METRIC_WAIT_S + 120)
def test_bank_stub_restart_mid_run_keeps_its_ledger(
    stack: Stack, toxiproxy: Toxiproxy, containers: Containers, payroll: Payroll
) -> None:
    """PAY-412, PAY-NFR-02, HLD §4 "bank-stub restart": the bank settles, then restarts before
    the worker hears back. Its ledger is persistent, so the redelivery with the same key finds the
    original transfer: at most one settled transfer per payout, and the run completes."""
    settled = stack.prom(SETTLED)
    run = payroll.calculated_run()
    toxiproxy.add("bank-stub", "lose-answers", "timeout", stream="downstream", timeout=0)
    payroll.approve(run["id"])
    ids = payout_ids(stack, run["id"])
    wait_until("the bank settles every transfer", lambda: settled_count(stack, ids) == len(ids), 60)
    assert disbursements(stack, ids, "submitting") == len(ids)
    before = transfers(stack, ids)

    restarted = datetime.now(UTC)
    containers.restart("bank-stub")
    assert transfers(stack, ids) == before  # the ledger survived the restart
    toxiproxy.remove("bank-stub", "lose-answers")

    payroll.wait_final(run["id"], REDELIVERY_S)
    assert_paid_once(stack, payroll, run["id"])
    assert transfers(stack, ids) == before
    # the restarted bank-stub answered each repeated key from its ledger, and the worker counted
    # one settled answer per payout (it heard none before the restart)
    replayed = log_events(stack.logs_since("bank-stub", restarted), "bank_stub.transfer.replayed")
    assert replayed >= len(ids)
    stack.wait_increase("settled bank answers", SETTLED, settled, len(ids))

"""Worker crash before and after the bank call (HLD §4 rows 1 and 2; plan §12.1; testing.md §8
"Worker crash mid-message"; PAY-403, PAY-412, PAY-NFR-02).

The worker is killed with SIGKILL while its messages are in flight, then started again. Its
consumer name is the container hostname, so the restarted process reads its own pending entries
first (worker LLD §3.3); XAUTOCLAIM would take them after 60 s otherwise. Either way each payout
ends paid with exactly one settled bank transfer.

- Before the bank call: a Toxiproxy ``timeout`` toxic on the request path to bank-stub holds every
  POST, so the worker has written ``submitting`` but the bank has seen nothing.
- After the bank call: the toxic sits on the response path, so the bank settles every transfer
  but the worker never hears back; the redelivery reuses the bank key ``<payout_id>:<attempt>``
  and the bank returns the original transfer.

Each test also checks the HLD §4 row's log event and metric, from the restarted process: its
``queue_messages_total`` / ``payouts_bank_transfers_total`` series are new (a new
``service.instance.id``), so they count in full.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _stack import (
    CLAIM_IDLE_S,
    METRIC_WAIT_S,
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

PROCESSED = 'queue_messages_total{service_name="payout-worker", outcome="ok"}'
SETTLED = 'payouts_bank_transfers_total{service_name="payout-worker", outcome="settled"}'

REDELIVERY_S = CLAIM_IDLE_S + 90  # pending-first on restart; XAUTOCLAIM as the fallback


def _in_flight(stack: Stack, payroll: Payroll, toxiproxy: Toxiproxy, stream: str) -> list[str]:
    """Approve a new run with bank traffic held on ``stream``; returns its payout ids once every
    payout is in the worker's hands (``submitting``)."""
    run = payroll.calculated_run()
    toxiproxy.add("bank-stub", "hold", "timeout", stream=stream, timeout=0)
    payroll.approve(run["id"])
    ids = payout_ids(stack, run["id"])
    assert ids
    wait_until(
        "every payout submitting in the worker",
        lambda: disbursements(stack, ids, "submitting") == len(ids),
        timeout=60,
    )
    return [run["id"], *ids]


@pytest.mark.timeout(300 + METRIC_WAIT_S)
def test_worker_crash_before_the_bank_call_is_redelivered_and_paid_once(
    stack: Stack, toxiproxy: Toxiproxy, containers: Containers, payroll: Payroll
) -> None:
    """PAY-403, PAY-NFR-02, HLD §4 "Worker crash before the bank call": redelivered, paid once."""
    run_id, *ids = _in_flight(stack, payroll, toxiproxy, "upstream")
    assert stack.pending() >= len(ids)
    assert transfers(stack, ids) == {i: [] for i in ids}  # the bank never saw a request

    processed = stack.prom(PROCESSED)
    containers.kill("payout-worker")
    toxiproxy.remove("bank-stub", "hold")
    restarted = datetime.now(UTC)
    containers.start("payout-worker")

    payroll.wait_final(run_id, REDELIVERY_S)
    assert_paid_once(stack, payroll, run_id)
    assert stack.pending() == 0
    # HLD §4: `queue.message.processed` / queue_messages_total, from the restarted worker
    logs = stack.logs_since("payout-worker", restarted)
    assert log_events(logs, "queue.message.processed") >= len(ids)
    stack.wait_increase("redelivered messages processed", PROCESSED, processed, len(ids))


@pytest.mark.timeout(300 + METRIC_WAIT_S)
def test_worker_crash_after_the_bank_call_reuses_the_key_and_pays_once(
    stack: Stack, toxiproxy: Toxiproxy, containers: Containers, payroll: Payroll
) -> None:
    """PAY-403, PAY-412, PAY-NFR-02, HLD §4 "Worker crash after the bank call": the bank settled
    every transfer but the worker never got the answer; after the crash the redelivery sends the
    same key and the bank returns the original transfer: one settled transfer per payout."""
    run_id, *ids = _in_flight(stack, payroll, toxiproxy, "downstream")
    wait_until("the bank settles every transfer", lambda: settled_count(stack, ids) == len(ids), 30)
    before = transfers(stack, ids)

    settled = stack.prom(SETTLED)
    containers.kill("payout-worker")
    toxiproxy.remove("bank-stub", "hold")
    restarted = datetime.now(UTC)
    containers.start("payout-worker")

    payroll.wait_final(run_id, REDELIVERY_S)
    assert_paid_once(stack, payroll, run_id)
    assert transfers(stack, ids) == before  # no new transfer: the original was returned
    # HLD §4: `bank.transfer.submitted` / payouts_bank_transfers_total, from the restarted worker;
    # the bank answered each repeated key with the stored transfer (bank_stub.transfer.replayed)
    logs = stack.logs_since("payout-worker", restarted)
    assert log_events(logs, "bank.transfer.submitted") >= len(ids)
    replayed = log_events(stack.logs_since("bank-stub", restarted), "bank_stub.transfer.replayed")
    assert replayed >= len(ids)
    stack.wait_increase("settled bank answers", SETTLED, settled, len(ids))
    for p in payroll.payouts(run_id):
        assert [a["attempt"] for a in p["attempts"]] == [1]  # same attempt, same key

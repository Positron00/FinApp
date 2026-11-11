"""Stateful payout model, first cut (design §9.4; PAY-NFR-02, PAY-405, PAY-406, PAY-411).

Drives the real domain functions (``decide_result``, ``instruction_allowed``,
``payout_status_for_attempt``, ``run_status_from_payouts``) through an in-memory harness that
mirrors ``services/record_result.py`` and the worker's per-message steps. S5 scope: one attempt
per payout, redelivery, duplicate consumers, crashes, bank timeouts before and after settling,
poison, re-publish of a live attempt, stream loss and stale results. Retry, resolve and the
re-publish backoff rules join the model in F4a.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    initialize,
    invariant,
    precondition,
    rule,
)
from payroll.domain.state import (
    FINAL_ATTEMPT,
    AttemptStatus,
    PayoutStatus,
    ResultKind,
    ResultOutcome,
    RunStatus,
    decide_result,
    instruction_allowed,
    payout_status_for_attempt,
    run_status_from_payouts,
)


@dataclass
class Payout:
    pid: int
    amount: int
    current_attempt: int  # 0 for no_transfer
    status: PayoutStatus
    attempts: dict[int, AttemptStatus] = field(default_factory=dict)
    final_seen: dict[int, AttemptStatus] = field(default_factory=dict)

    @property
    def no_transfer(self) -> bool:
        return self.amount == 0


@dataclass
class Message:
    event: int
    pid: int
    attempt: int


@dataclass
class Consumer:
    msg: Message
    step: str = "delivered"  # delivered → fetched → called (bank_answered?) → done
    bank_answered: bool = False
    outcome: ResultOutcome | None = None


class PayoutModel(RuleBasedStateMachine):
    consumers = Bundle("consumers")

    def __init__(self) -> None:
        super().__init__()
        self.payouts: dict[int, Payout] = {}
        self.run_status = RunStatus.CALCULATED
        self.bank: dict[tuple[int, int], str] = {}  # key → "settled" | "rejected"
        self.stream: list[Message] = []
        self.next_event = 0

    # --- harness mirroring the services layer ----------------------------------------------

    def _publish(self, pid: int, attempt: int) -> None:
        self.next_event += 1
        self.stream.append(Message(self.next_event, pid, attempt))

    def _record(self, pid: int, attempt: int, outcome: ResultOutcome) -> None:
        """services/record_result.py: decide, apply, recompute the run status."""
        p = self.payouts[pid]
        d = decide_result(
            current_attempt=p.current_attempt,
            attempt=attempt,
            attempt_status=p.attempts.get(attempt),
            outcome=outcome,
        )
        if d.kind is not ResultKind.APPLY or d.new_status is None:
            return
        p.attempts[attempt] = d.new_status
        p.status = payout_status_for_attempt(d.new_status)
        if d.new_status in FINAL_ATTEMPT:
            p.final_seen.setdefault(attempt, d.new_status)
        self.run_status = run_status_from_payouts(x.status for x in self.payouts.values())

    # --- rules -------------------------------------------------------------------------------

    @initialize(amounts=st.lists(st.integers(min_value=0, max_value=3), min_size=1, max_size=3))
    def approve(self, amounts: list[int]) -> None:
        """PAY-400: one payout per payslip; net 0 → paid + no_transfer, else attempt 1."""
        for pid, amount in enumerate(amounts):
            if amount == 0:
                self.payouts[pid] = Payout(pid, 0, 0, PayoutStatus.PAID)
            else:
                self.payouts[pid] = Payout(
                    pid, amount, 1, PayoutStatus.REQUESTED, {1: AttemptStatus.REQUESTED}
                )
                self._publish(pid, 1)
        self.run_status = run_status_from_payouts(p.status for p in self.payouts.values())

    @precondition(lambda self: bool(self.stream))
    @rule(target=consumers, index=st.integers(min_value=0, max_value=50))
    def deliver(self, index: int) -> Consumer:
        """Start or redeliver a pending message, possibly a second consumer on the same one."""
        return Consumer(self.stream[index % len(self.stream)])

    @rule(
        c=consumers,
        answer=st.sampled_from(["settled", "rejected"]),
        mode=st.sampled_from([0, 0, 0, 1, 2]),
        steps=st.integers(min_value=1, max_value=3),
    )
    def advance(self, c: Consumer, answer: str, mode: int, steps: int) -> None:
        """Take one consumer up to ``steps`` steps on: instruction, bank call, post result."""
        for _ in range(steps):
            if c.step == "delivered":
                self._fetch_instruction(c)
            elif c.step == "fetched":
                self._bank_call(c, answer, mode)
            elif c.step == "called":
                self._post_result(c)

    def _fetch_instruction(self, c: Consumer) -> None:
        p = self.payouts[c.msg.pid]
        allowed = instruction_allowed(
            current_attempt=p.current_attempt,
            attempt=c.msg.attempt,
            attempt_status=p.attempts.get(c.msg.attempt),
        )
        c.step = "fetched" if allowed else "done"  # 409 → ack, nothing sent to the bank

    def _bank_call(self, c: Consumer, answer: str, mode: int) -> None:
        """mode 0: answers; 1: times out after settling; 2: times out before reaching the bank."""
        key = (c.msg.pid, c.msg.attempt)
        if mode in (0, 1):
            stored = self.bank.setdefault(key, answer)  # the key: first write wins
            if mode == 0:
                c.bank_answered = True
                c.outcome = ResultOutcome(stored)
        if not c.bank_answered:
            c.outcome = ResultOutcome.INDETERMINATE  # delivery budget spent
        c.step = "called"

    def _post_result(self, c: Consumer) -> None:
        if c.outcome is not None:
            self._record(c.msg.pid, c.msg.attempt, c.outcome)
        c.step = "done"

    @rule(c=consumes(consumers))
    def crash_consumer(self, c: Consumer) -> None:
        """A crash at any step drops the in-flight state; the message stays pending."""

    @precondition(lambda self: bool(self.stream))
    @rule(index=st.integers(min_value=0, max_value=50))
    def ack(self, index: int) -> None:
        self.stream.pop(index % len(self.stream))

    @rule(
        pid=st.integers(min_value=0, max_value=2),
        reason=st.sampled_from(["schema_invalid", "amount_mismatch"]),
    )
    def poison(self, pid: int, reason: str) -> None:
        """PAY-409: the worker dead-letters a message and reports poison for the current attempt."""
        if pid in self.payouts and not self.payouts[pid].no_transfer:
            self._record(pid, self.payouts[pid].current_attempt, ResultOutcome.POISON)

    @rule(pid=st.integers(min_value=0, max_value=2))
    def republish(self, pid: int) -> None:
        """PAY-407 (shape only): a live current attempt is re-published with a fresh event_id."""
        p = self.payouts.get(pid)
        if p is not None and p.attempts.get(p.current_attempt) in (
            AttemptStatus.REQUESTED,
            AttemptStatus.INDETERMINATE,
        ):
            self._publish(pid, p.current_attempt)

    @rule()
    def flush_stream(self) -> None:
        """Redis data loss: every in-flight message disappears."""
        self.stream.clear()

    @rule(pid=st.integers(min_value=0, max_value=2), outcome=st.sampled_from(list(ResultOutcome)))
    def late_result_for_old_attempt(self, pid: int, outcome: ResultOutcome) -> None:
        """A stale attempt number is always NOT_CURRENT and changes nothing."""
        if pid in self.payouts:
            before = (self.payouts[pid].status, dict(self.payouts[pid].attempts))
            self._record(pid, self.payouts[pid].current_attempt + 1, outcome)
            assert (self.payouts[pid].status, self.payouts[pid].attempts) == before

    # --- invariants (design §9.4) ------------------------------------------------------------

    @invariant()
    def at_most_one_settled_key_per_payout(self) -> None:
        """PAY-NFR-02 (1): at most one settled bank key per payout across attempts."""
        for pid in self.payouts:
            settled = [k for k, v in self.bank.items() if k[0] == pid and v == "settled"]
            assert len(settled) <= 1

    @invariant()
    def paid_iff_no_transfer_or_the_settled_attempt(self) -> None:
        """PAY-NFR-02 (2): paid ⇔ no_transfer with zero transfers, or exactly one settled
        transfer and the recorded attempt is that one."""
        for p in self.payouts.values():
            transfers = [k for k in self.bank if k[0] == p.pid]
            if p.no_transfer:
                assert p.status is PayoutStatus.PAID
                assert not transfers
                continue
            if p.status is PayoutStatus.PAID:
                key = (p.pid, p.current_attempt)
                assert self.bank.get(key) == "settled"
                assert p.attempts[p.current_attempt] is AttemptStatus.SETTLED

    @invariant()
    def failed_has_no_settled_transfer(self) -> None:
        """PAY-NFR-02 (3): failed ⇒ the current attempt's key was not settled at the bank."""
        for p in self.payouts.values():
            if p.status is PayoutStatus.FAILED:
                assert self.bank.get((p.pid, p.current_attempt)) != "settled"

    @invariant()
    def run_status_matches_the_rule(self) -> None:
        """PAY-406 (5): the stored run status equals run_status_from_payouts."""
        if self.payouts:
            expected = run_status_from_payouts(p.status for p in self.payouts.values())
            assert self.run_status is expected

    @invariant()
    def final_attempts_never_change(self) -> None:
        """PAY-405 (6): a final attempt's status never changes."""
        for p in self.payouts.values():
            for attempt, status in p.final_seen.items():
                assert p.attempts[attempt] is status


PayoutModel.TestCase.settings = settings(max_examples=200, stateful_step_count=40, deadline=None)
TestPayoutModel = PayoutModel.TestCase

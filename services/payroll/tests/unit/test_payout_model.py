"""Stateful payout model (design §9.4; PAY-NFR-02, PAY-NFR-03, PAY-405, PAY-406, PAY-407,
PAY-411, PAY-500, PAY-502, C48).

Drives the real domain functions (``decide_result``, ``instruction_allowed``,
``payout_status_for_attempt``, ``run_status_from_payouts``, ``check_retry``,
``republish_decision``, ``check_resolve``, ``check_mark_rejected_wait``, ``resolve_transition``)
through an in-memory harness that mirrors the services layer (record_result, retry, resolve, the
re-publish task) and the worker's per-message steps. A fake clock in whole seconds drives the
re-publish backoff and the C48 wait; the worker never starts a bank POST more than
``HANDLER_DEADLINE_S`` after its instruction fetch. The teardown drains everything against a
healthy bank and checks liveness (PAY-NFR-03).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from hypothesis import event, settings
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
from payroll.domain import DomainError, ResolveTooEarly
from payroll.domain.resolve import (
    ResolveAction,
    check_mark_rejected_wait,
    check_resolve,
    resolve_transition,
)
from payroll.domain.retry import RepublishAction, check_retry, republish_decision
from payroll.domain.state import (
    FINAL_ATTEMPT,
    FINAL_RUN,
    LIVE_ATTEMPT,
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

EPOCH: Final = datetime(2026, 11, 11, tzinfo=UTC)
EMPLOYEE_ID: Final = UUID(int=1)
MAX_ATTEMPTS: Final = 5
AFTER_S: Final = 360
MAX_BACKOFF_S: Final = 900
MAX_REPUBLISH: Final = 10
MIN_WAIT_S: Final = 120
HANDLER_DEADLINE_S: Final = 35
PIDS = st.integers(min_value=0, max_value=5)  # mapped onto existing payouts by _pid


@dataclass
class Attempt:
    status: AttemptStatus
    publish_count: int = 1
    last_published_at: int = 0
    needs_operator_at: int | None = None


@dataclass
class Payout:
    pid: int
    amount: int
    current_attempt: int  # 0 for no_transfer
    status: PayoutStatus
    attempts: dict[int, Attempt] = field(default_factory=dict)
    final_seen: dict[int, AttemptStatus] = field(default_factory=dict)
    employee_ok: bool = True  # active with bank details

    @property
    def no_transfer(self) -> bool:
        return self.amount == 0

    def current(self) -> Attempt | None:
        return self.attempts.get(self.current_attempt)


@dataclass
class Message:
    event: int
    pid: int
    attempt: int


@dataclass
class Consumer:
    msg: Message
    step: str = "delivered"  # delivered → fetched → called (bank_answered?) → done
    fetched_at: int = 0
    bank_answered: bool = False
    outcome: ResultOutcome | None = None


class PayoutModel(RuleBasedStateMachine):
    consumers = Bundle("consumers")

    def __init__(self) -> None:
        super().__init__()
        self.clock = 0
        self.payouts: dict[int, Payout] = {}
        self.run_status = RunStatus.CALCULATED
        self.bank: dict[tuple[int, int], str] = {}  # key → "settled" | "rejected"
        self.stream: list[Message] = []
        self.processed_events: set[int] = set()
        self.next_event = 0
        self.first_final_seen = False  # pay_runs.first_final_at IS NOT NULL
        self.settlement_observations = 0
        self.ever_final = False
        self.retried_from: list[AttemptStatus] = []  # the current attempt's status at each retry
        self.resolved_from: list[AttemptStatus] = []

    def _pid(self, i: int) -> int:
        """Map a drawn index onto an existing payout, so every draw targets one."""
        ids = sorted(self.payouts)
        return ids[i % len(ids)]

    def _now(self) -> datetime:
        return EPOCH + timedelta(seconds=self.clock)

    # --- harness mirroring the services layer ----------------------------------------------

    def _publish(self, pid: int, attempt: int) -> None:
        """An outbox row, relayed at once with a fresh event_id."""
        self.next_event += 1
        self.stream.append(Message(self.next_event, pid, attempt))

    def _record(self, pid: int, attempt: int, outcome: ResultOutcome) -> None:
        """services/record_result.py: decide, apply, recompute the run status."""
        p = self.payouts[pid]
        a = p.attempts.get(attempt)
        d = decide_result(
            current_attempt=p.current_attempt,
            attempt=attempt,
            attempt_status=a.status if a is not None else None,
            outcome=outcome,
        )
        if d.kind is not ResultKind.APPLY or d.new_status is None or a is None:
            return
        a.status = d.new_status
        a.needs_operator_at = self.clock if d.new_status is AttemptStatus.NEEDS_OPERATOR else None
        p.status = payout_status_for_attempt(d.new_status)
        if d.new_status in FINAL_ATTEMPT:
            p.final_seen.setdefault(attempt, d.new_status)
        self._recompute_run_status()

    def _recompute_run_status(self) -> None:
        """services/run_status.py: first final entry sets first_final_at and observes once."""
        new = run_status_from_payouts(x.status for x in self.payouts.values())
        if new is self.run_status:
            return
        self.run_status = new
        if new in FINAL_RUN and not self.first_final_seen:
            self.first_final_seen = True
            self.settlement_observations += 1

    # --- rules: approve and the worker -------------------------------------------------------

    @initialize(amounts=st.lists(st.integers(min_value=0, max_value=3), min_size=1, max_size=3))
    def approve(self, amounts: list[int]) -> None:
        """PAY-400: one payout per payslip; net 0 → paid + no_transfer, else attempt 1."""
        for pid, amount in enumerate(amounts):
            if amount == 0:
                self.payouts[pid] = Payout(pid, 0, 0, PayoutStatus.PAID)
            else:
                self.payouts[pid] = Payout(
                    pid, amount, 1, PayoutStatus.REQUESTED, {1: Attempt(AttemptStatus.REQUESTED)}
                )
                self._publish(pid, 1)
        self._recompute_run_status()

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
        if c.msg.event in self.processed_events:
            c.step = "done"  # processed_events: a duplicate is acked without work
            return
        p = self.payouts[c.msg.pid]
        a = p.attempts.get(c.msg.attempt)
        allowed = instruction_allowed(
            current_attempt=p.current_attempt,
            attempt=c.msg.attempt,
            attempt_status=a.status if a is not None else None,
        )
        c.fetched_at = self.clock
        c.step = "fetched" if allowed else "done"  # 409 → ack, nothing sent to the bank

    def _bank_call(self, c: Consumer, answer: str, mode: int) -> None:
        """mode 0: answers; 1: times out after settling; 2: times out before reaching the bank.
        The worker never starts a POST later than HANDLER_DEADLINE_S after the fetch (C48)."""
        if self.clock - c.fetched_at > HANDLER_DEADLINE_S:
            c.step = "done"  # handler deadline passed: abandoned, the message stays pending
            return
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
            self.processed_events.add(c.msg.event)
        c.step = "done"

    @rule(c=consumes(consumers))
    def crash_consumer(self, c: Consumer) -> None:
        """A crash at any step drops the in-flight state; the message stays pending."""

    @precondition(lambda self: bool(self.stream))
    @rule(index=st.integers(min_value=0, max_value=50))
    def ack(self, index: int) -> None:
        self.stream.pop(index % len(self.stream))

    @rule(pid=PIDS, reason=st.sampled_from(["schema_invalid", "amount_mismatch"]))
    def poison(self, pid: int, reason: str) -> None:
        """PAY-409: the worker dead-letters a message and reports poison for the current attempt."""
        pid = self._pid(pid)
        if pid in self.payouts and not self.payouts[pid].no_transfer:
            self._record(pid, self.payouts[pid].current_attempt, ResultOutcome.POISON)

    @rule(
        c=consumers,
        action=st.sampled_from(list(ResolveAction)),
        wait=st.sampled_from([0, 30, 121]),
        answer=st.sampled_from(["settled", "rejected"]),
    )
    def operator_races_inflight_handler(
        self, c: Consumer, action: ResolveAction, wait: int, answer: str
    ) -> None:
        """C48: a handler fetches its instruction; another delivery of the payout is poisoned;
        after ``wait`` seconds an operator resolves it; then the handler tries its bank POST.
        The wait plus the handler deadline mean a mark_rejected can never be followed by a
        settled POST for the same key."""
        if c.step == "delivered":
            self._fetch_instruction(c)
        self._record(c.msg.pid, self.payouts[c.msg.pid].current_attempt, ResultOutcome.POISON)
        self.clock += wait
        self._resolve(c.msg.pid, action)
        if c.step == "fetched":
            self._bank_call(c, answer, 1)

    @rule(pid=PIDS)
    def exhaust_budget(self, pid: int) -> None:
        """PAY-408: deliveries exhausted on bank failures → indeterminate for the attempt."""
        pid = self._pid(pid)
        if pid in self.payouts and not self.payouts[pid].no_transfer:
            self._record(pid, self.payouts[pid].current_attempt, ResultOutcome.INDETERMINATE)

    @rule()
    def flush_stream(self) -> None:
        """Redis data loss: every in-flight message disappears."""
        self.stream.clear()

    @rule(pid=PIDS, outcome=st.sampled_from(list(ResultOutcome)))
    def late_result_for_old_attempt(self, pid: int, outcome: ResultOutcome) -> None:
        """A stale attempt number is always NOT_CURRENT and changes nothing."""
        pid = self._pid(pid)
        if pid in self.payouts:
            p = self.payouts[pid]
            before = (p.status, {k: a.status for k, a in p.attempts.items()})
            self._record(pid, p.current_attempt + 1, outcome)
            if p.current_attempt > 1:
                self._record(pid, p.current_attempt - 1, outcome)
            assert (p.status, {k: a.status for k, a in p.attempts.items()}) == before

    # --- rules: time and maintenance -------------------------------------------------------

    @rule(seconds=st.sampled_from([1, 20, 40, 121, 400, 901]))
    def advance_clock(self, seconds: int) -> None:
        self.clock += seconds

    @rule(rounds=st.integers(min_value=1, max_value=12))
    def bank_silent_for(self, rounds: int) -> None:
        """Time passes with no worker progress: each round advances past the backoff cap and
        runs a maintenance tick, so the re-publish limit (PAYOUT_REPUBLISH_MAX) is reachable."""
        for _ in range(rounds):
            self.clock += MAX_BACKOFF_S + 1
            self.maintenance_tick()

    @rule()
    def maintenance_tick(self) -> None:
        """PAY-407 (events/republish.py): re-publish with a fresh event_id after the backoff;
        needs_operator after PAYOUT_REPUBLISH_MAX re-publishes. The relay sends at once, so no
        unsent outbox row ever blocks."""
        for p in self.payouts.values():
            a = p.current()
            if a is None:
                continue
            action = republish_decision(
                status=a.status,
                is_current=True,
                publish_count=a.publish_count,
                last_published_at=EPOCH + timedelta(seconds=a.last_published_at),
                now=self._now(),
                has_unsent_outbox=False,
                after_s=AFTER_S,
                max_backoff_s=MAX_BACKOFF_S,
                max_republish=MAX_REPUBLISH,
            )
            if action is RepublishAction.REPUBLISH:
                assert a.status in LIVE_ATTEMPT
                self._publish(p.pid, p.current_attempt)
                a.publish_count += 1
                a.last_published_at = self.clock
            elif action is RepublishAction.NEEDS_OPERATOR:
                event("republish_limit")
                a.status = AttemptStatus.NEEDS_OPERATOR
                a.needs_operator_at = self.clock  # run status unchanged: payout stays requested

    # --- rules: operator actions -------------------------------------------------------------

    @rule(pid=PIDS)
    def purge_bank_details(self, pid: int) -> None:
        """PAY-608 / deactivation: the employee no longer has usable bank details."""
        pid = self._pid(pid)
        if pid in self.payouts:
            self.payouts[pid].employee_ok = False

    @rule(pid=PIDS)
    def retry(self, pid: int) -> None:
        """PAY-500 (services/retry.py): only through check_retry; attempt n+1 is published."""
        pid = self._pid(pid)
        p = self.payouts.get(pid)
        if p is None:
            return
        a = p.current()
        try:
            check_retry(
                payout_status=p.status,
                attempt_status=a.status if a is not None else None,
                current_attempt=p.current_attempt,
                max_attempts=MAX_ATTEMPTS,
                employee_id=EMPLOYEE_ID,
                employee_active=p.employee_ok,
                employee_has_bank=p.employee_ok,
            )
        except DomainError:
            return
        assert a is not None
        self.retried_from.append(a.status)
        event("retry")
        p.current_attempt += 1
        p.attempts[p.current_attempt] = Attempt(
            AttemptStatus.REQUESTED, last_published_at=self.clock
        )
        p.status = PayoutStatus.REQUESTED
        self._publish(pid, p.current_attempt)
        self._recompute_run_status()

    @rule(pid=PIDS, action=st.sampled_from(list(ResolveAction)))
    def resolve(self, pid: int, action: ResolveAction) -> None:
        """PAY-502 (services/resolve.py): only through check_resolve. mark_rejected waits
        PAYOUT_MARK_REJECTED_MIN_WAIT_S after needs_operator_at (C48) and follows the runbook:
        the attempt's bank key has no settled transfer."""
        self._resolve(self._pid(pid), action)

    def _resolve(self, pid: int, action: ResolveAction) -> None:
        p = self.payouts.get(pid)
        if p is None:
            return
        a = p.current()
        try:
            check_resolve(payout_status=p.status, attempt_status=a.status if a else None)
        except DomainError:
            return
        assert a is not None
        assert a.needs_operator_at is not None
        t = resolve_transition(action)
        if action is ResolveAction.MARK_REJECTED:
            try:
                check_mark_rejected_wait(
                    needs_operator_at=EPOCH + timedelta(seconds=a.needs_operator_at),
                    now=self._now(),
                    min_wait_s=MIN_WAIT_S,
                )
            except ResolveTooEarly:
                return
            if self.bank.get((pid, p.current_attempt)) == "settled":
                return  # the runbook: GET /v1/transfers/{key} shows a settled transfer
        self.resolved_from.append(a.status)
        event(f"resolve:{action}")
        a.status = t.attempt_status
        a.needs_operator_at = None
        p.status = t.payout_status
        if t.attempt_final:
            p.final_seen.setdefault(p.current_attempt, a.status)
        if t.publish:
            assert t.publish_count is not None
            a.publish_count = t.publish_count
            a.last_published_at = self.clock
            self._publish(pid, p.current_attempt)
        self._recompute_run_status()

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
                assert self.bank.get((p.pid, p.current_attempt)) == "settled"
                assert p.attempts[p.current_attempt].status is AttemptStatus.SETTLED

    @invariant()
    def failed_has_no_settled_transfer(self) -> None:
        """PAY-NFR-02 (3), C48: failed ⇒ the current attempt's key was not settled at the bank,
        including after an operator mark_rejected (no in-flight POST can still settle it)."""
        for p in self.payouts.values():
            if p.status is PayoutStatus.FAILED:
                assert self.bank.get((p.pid, p.current_attempt)) != "settled"

    @invariant()
    def retry_and_resolve_only_from_their_states(self) -> None:
        """PAY-500, PAY-502 (4): retries only from rejected; resolves only from needs_operator."""
        assert all(s is AttemptStatus.REJECTED for s in self.retried_from)
        assert all(s is AttemptStatus.NEEDS_OPERATOR for s in self.resolved_from)
        for p in self.payouts.values():
            assert p.current_attempt <= MAX_ATTEMPTS
            for n, a in p.attempts.items():
                if n < p.current_attempt:
                    assert a.status in FINAL_ATTEMPT  # an older attempt is always final

    @invariant()
    def run_status_matches_the_rule(self) -> None:
        """PAY-406 (5): the stored run status equals the plan's rule, written out here
        independently of run_status_from_payouts."""
        if not self.payouts:
            return
        statuses = [p.status for p in self.payouts.values()]
        if PayoutStatus.REQUESTED in statuses:
            expected = RunStatus.PROCESSING
        elif set(statuses) == {PayoutStatus.PAID}:
            expected = RunStatus.PAID
        else:
            expected = RunStatus.PARTIALLY_FAILED
        assert self.run_status is expected

    @invariant()
    def settlement_is_observed_once(self) -> None:
        """PAY-406: the settlement histogram is observed once the run has been final, never
        before, and never again after a retry re-opens and re-closes it."""
        if self.payouts and self.run_status is not RunStatus.PROCESSING:
            self.ever_final = True
        assert self.settlement_observations == (1 if self.ever_final else 0)

    @invariant()
    def final_attempts_never_change(self) -> None:
        """PAY-405 (6): a final attempt's status never changes."""
        for p in self.payouts.values():
            for attempt, status in p.final_seen.items():
                assert p.attempts[attempt].status is status

    @invariant()
    def needs_operator_at_is_set_iff_blocked(self) -> None:
        """C48: needs_operator_at is set exactly while the attempt is needs_operator."""
        for p in self.payouts.values():
            for a in p.attempts.values():
                assert (a.status is AttemptStatus.NEEDS_OPERATOR) == (
                    a.needs_operator_at is not None
                )

    # --- liveness (7) ------------------------------------------------------------------------

    def teardown(self) -> None:
        """PAY-NFR-03 (7): with a healthy bank (it always answers; a new key settles), draining
        the stream and ticking maintenance until quiet leaves every payout final or blocked
        (needs_operator); an attempt that was live with re-publish budget left must end final,
        since re-publishing reaches the healthy bank before the limit. The safety invariants
        still hold afterwards."""
        must_finish = {
            p.pid
            for p in self.payouts.values()
            if (a := p.current()) is not None
            and a.status in LIVE_ATTEMPT
            and a.publish_count - 1 < MAX_REPUBLISH
        }
        for _ in range(4 * (MAX_REPUBLISH + 2)):
            while self.stream:
                c = Consumer(self.stream.pop(0))
                self._fetch_instruction(c)
                if c.step == "fetched":
                    self._bank_call(c, "settled", 0)
                    self._post_result(c)
            live = [
                p for p in self.payouts.values() if (a := p.current()) and a.status in LIVE_ATTEMPT
            ]
            if not live:
                break
            self.clock += MAX_BACKOFF_S + 1
            self.maintenance_tick()
        for p in self.payouts.values():
            a = p.current()
            if p.pid in must_finish:
                assert p.status in (PayoutStatus.PAID, PayoutStatus.FAILED), (p.pid, a)
            assert p.status in (PayoutStatus.PAID, PayoutStatus.FAILED) or (
                a is not None and a.status is AttemptStatus.NEEDS_OPERATOR
            ), (p.pid, p.status, a)
        self.at_most_one_settled_key_per_payout()
        self.paid_iff_no_transfer_or_the_settled_attempt()
        self.failed_has_no_settled_transfer()
        self.run_status_matches_the_rule()


PayoutModel.TestCase.settings = settings(max_examples=500, stateful_step_count=60, deadline=None)
TestPayoutModel = PayoutModel.TestCase

"""Record a worker result (design §4.7; PAY-405, PAY-406, PAY-409, PAY-NFR-02, R7, C26, C48).

Lock order: idempotency row (1) → run (3) → payout (4) → attempt (5). ``decide_result`` picks
NOT_CURRENT (409, rolled back), STORED (200, unchanged) or APPLY (conditional attempt update,
payout status, run status, audit). The response ``outcome`` is the stored attempt status.

**Late settlement** (``is_late_settlement``, design §4.7): a ``settled`` answer for a current
attempt already ``rejected`` (STORED) or for an earlier, non-settled attempt (NOT_CURRENT) means
the bank may have paid twice. The response is unchanged (200 stored outcome / 409), but the
event is never dropped: an audit row ``payout.late_settlement`` (once per payout and attempt;
in the result transaction for STORED, in its own transaction after the 409's rollback for
NOT_CURRENT), an error log ``payout.late_settlement`` and ``payroll_late_settlements_total``
(the ``PayoutLateSettlement`` page).
"""

# ruff: noqa: N818  (the design's error name)

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from opentelemetry import trace
from pycommon.auth import Principal
from pycommon.errors import AppError
from pycommon.logging import get_logger, log_event
from pycommon.otel import tracer
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from payroll.api.schemas import ResultIn, ResultOut
from payroll.db.models import AuditEvent, Payout, PayoutAttempt
from payroll.db.repositories import AttemptRepo, AuditRepo, PayoutRepo, PayRunRepo, rowcount
from payroll.db.session import session_scope
from payroll.domain import AttemptNotCurrent
from payroll.domain.state import (
    FINAL_ATTEMPT,
    AttemptStatus,
    PayoutStatus,
    ResultKind,
    ResultOutcome,
    decide_result,
    is_late_settlement,
    payout_status_for_attempt,
)
from payroll.services import idempotency
from payroll.services.run_status import RunTransition, recompute_run_status, report_transition
from payroll.services.runtime import Runtime

__all__ = ["ConcurrentUpdate", "ResultResponse", "record_result"]

_log: Final = get_logger("payroll.payouts")
POISON_REASON: Final = "poison"
LATE_SETTLEMENT_EVENT: Final = "payout.late_settlement"
WORKER_ACTOR: Final = "service:payout-worker"
_OUTCOME_METRIC: Final = {
    AttemptStatus.SETTLED: "paid",
    AttemptStatus.REJECTED: "failed",
    AttemptStatus.INDETERMINATE: "indeterminate",
    AttemptStatus.NEEDS_OPERATOR: "needs_operator",
}


class ConcurrentUpdate(AppError):
    """A guarded UPDATE matched no row although the locks were held: a bug (500)."""


@dataclass(frozen=True)
class ResultResponse:
    status: int
    body: Any
    replayed: bool


@dataclass(frozen=True)
class _LateFacts:
    """What the result transaction saw of a late ``settled`` answer (PAY-NFR-02)."""

    pay_run_id: UUID
    attempt: int
    current_attempt: int
    attempt_status: AttemptStatus
    failure_reason: str | None
    bank_reference: str | None


@dataclass(frozen=True)
class _Late:
    facts: _LateFacts
    later_settled_attempt: int | None
    recorded: bool  # False: this payout and attempt were already recorded


class _LateNotCurrent(AttemptNotCurrent):
    """The 409 of a late settlement on the NOT_CURRENT path; carries what to record after the
    rollback. The API maps it exactly like ``AttemptNotCurrent``."""

    def __init__(self, current_attempt: int, facts: _LateFacts) -> None:
        super().__init__(current_attempt)
        self.facts = facts


@dataclass(frozen=True)
class _Applied:
    pay_run_id: UUID
    attempt_status: AttemptStatus
    payout_status: PayoutStatus
    run: RunTransition | None
    applied: bool
    failure_reason: str | None
    publish_count: int
    late: _Late | None = None


def _failure_reason(body: ResultIn, new: AttemptStatus) -> str | None:
    if new is AttemptStatus.REJECTED:
        return body.reason
    if new is AttemptStatus.NEEDS_OPERATOR:
        return body.reason or POISON_REASON
    return None


async def record_result(
    rt: Runtime,
    principal: Principal,
    payout_id: UUID,
    body: ResultIn,
    idem: idempotency.IdemRequest,
) -> ResultResponse:
    cid = principal.cid
    outcome = ResultOutcome(body.outcome)
    with tracer("payroll").start_as_current_span("payroll.payout.result") as span:
        span.set_attribute("payroll.payout_id", str(payout_id))
        span.set_attribute("payroll.attempt", body.attempt)
        span.set_attribute("payroll.outcome", str(outcome))
        try:
            async with session_scope(rt.sessions, "payouts.result") as session:
                replay = await idempotency.begin(session, idem)
                if replay is not None:
                    span.set_attribute("payroll.decision", "replayed")
                    return ResultResponse(replay.status, replay.body, replayed=True)
                applied = await _record_in_tx(
                    session, cid=cid, payout_id=payout_id, body=body, outcome=outcome, span=span
                )
                out = ResultOut(
                    payout_id=payout_id, attempt=body.attempt, outcome=applied.attempt_status
                )
                response = jsonable_encoder(out)
                await idempotency.store(session, idem, 200, response)
        except _LateNotCurrent as exc:
            # The 409 rolled the result transaction back; record the late settlement in its own
            # transaction. If that fails the error (503) replaces the 409, so the worker's retry
            # detects it again: it is never dropped.
            async with session_scope(rt.sessions, "payouts.late_settlement") as session:
                await PayoutRepo.lock(session, cid, payout_id)  # serialises the once-only check
                late = await _record_late(session, cid=cid, payout_id=payout_id, facts=exc.facts)
            span.set_attribute("payroll.late_settlement", True)
            _report_late(rt, payout_id, late)
            raise
    _after_commit(rt, payout_id, body, outcome, applied)
    return ResultResponse(200, response, replayed=False)


async def _record_in_tx(
    session: AsyncSession,
    *,
    cid: UUID,
    payout_id: UUID,
    body: ResultIn,
    outcome: ResultOutcome,
    span: trace.Span,
) -> _Applied:
    unlocked = await PayoutRepo.get(session, cid, payout_id)  # 404 for another cid
    run = await PayRunRepo.get(session, cid, unlocked.pay_run_id, for_update=True)
    payout = await PayoutRepo.lock(session, cid, payout_id)
    attempt = await AttemptRepo.lock(session, cid, payout_id, body.attempt)
    current = AttemptStatus(attempt.status) if attempt is not None else None
    decision = decide_result(
        current_attempt=payout.current_attempt,
        attempt=body.attempt,
        attempt_status=current,
        outcome=outcome,
    )
    span.set_attribute("payroll.decision", str(decision.kind))
    late_facts: _LateFacts | None = None
    if (
        attempt is not None
        and current is not None
        and is_late_settlement(
            current_attempt=payout.current_attempt,
            attempt=body.attempt,
            attempt_status=current,
            outcome=outcome,
        )
    ):
        late_facts = _LateFacts(
            pay_run_id=run.id,
            attempt=body.attempt,
            current_attempt=payout.current_attempt,
            attempt_status=current,
            failure_reason=attempt.failure_reason,
            bank_reference=body.bank_reference,
        )
    if decision.kind is ResultKind.NOT_CURRENT or attempt is None or current is None:
        log_event(
            _log, logging.INFO, "payout.attempt_not_current", payout_id=str(payout_id),
            attempt=body.attempt, current_attempt=payout.current_attempt, source="result",
        )  # fmt: skip
        if late_facts is not None:
            raise _LateNotCurrent(payout.current_attempt, late_facts)
        raise AttemptNotCurrent(payout.current_attempt)
    if decision.kind is ResultKind.STORED or decision.new_status is None:
        late = None
        if late_facts is not None:
            late = await _record_late(session, cid=cid, payout_id=payout_id, facts=late_facts)
            span.set_attribute("payroll.late_settlement", True)
        return _Applied(
            pay_run_id=run.id,
            attempt_status=current,
            payout_status=PayoutStatus(payout.status),
            run=None,
            applied=False,
            failure_reason=attempt.failure_reason,
            publish_count=attempt.publish_count,
            late=late,
        )

    new = decision.new_status
    reason = _failure_reason(body, new)
    final = new in FINAL_ATTEMPT
    result = await session.execute(
        update(PayoutAttempt)
        .where(PayoutAttempt.id == attempt.id, PayoutAttempt.status == str(current))
        .values(
            status=str(new),
            failure_reason=reason,
            bank_reference=body.bank_reference if new is AttemptStatus.SETTLED else None,
            final_at=func.now() if final else None,
            # Set on entry to needs_operator (C48). Every other target leaves needs_operator,
            # or comes from requested/indeterminate where it is already NULL (its CHECK).
            # clock_timestamp(), not the transaction start: the C48 wait counts from (close
            # to) the commit that makes the attempt non-live (design §5.3 "Time source").
            needs_operator_at=(
                func.clock_timestamp() if new is AttemptStatus.NEEDS_OPERATOR else None
            ),
        )
        .execution_options(synchronize_session=False)
    )
    if rowcount(result) != 1:  # pragma: no cover  (the row locks make this unreachable)
        raise ConcurrentUpdate()
    payout_status = payout_status_for_attempt(new)
    if payout_status != PayoutStatus(payout.status):
        await session.execute(
            update(Payout)
            .where(Payout.id == payout_id)
            .values(
                status=str(payout_status),
                final_at=func.now() if payout_status is not PayoutStatus.REQUESTED else None,
            )
            .execution_options(synchronize_session=False)
        )
    transition = await recompute_run_status(session, cid, run)
    details: dict[str, Any] = {"attempt": body.attempt, "outcome": str(outcome), "applied": True}
    if reason is not None:
        details["failure_reason"] = reason
    await AuditRepo.add(
        session, cid, actor_id=WORKER_ACTOR, event="payout.result_recorded",
        entity_type="payout", entity_id=payout_id, details=details,
    )  # fmt: skip
    if new is AttemptStatus.NEEDS_OPERATOR:
        await AuditRepo.add(
            session, cid, actor_id=WORKER_ACTOR, event="payout.needs_operator",
            entity_type="payout", entity_id=payout_id,
            details={"attempt": body.attempt, "reason": reason},
        )  # fmt: skip
    return _Applied(
        pay_run_id=run.id,
        attempt_status=new,
        payout_status=payout_status,
        run=transition,
        applied=True,
        failure_reason=reason,
        publish_count=attempt.publish_count,
    )


async def _record_late(
    session: AsyncSession, *, cid: UUID, payout_id: UUID, facts: _LateFacts
) -> _Late:
    """Write the ``payout.late_settlement`` audit row unless this payout and attempt already have
    one (the caller holds the payout lock). ``later_attempt_settled`` says whether a later
    attempt of the payout is ``settled`` too, i.e. the employee was very likely paid twice."""
    later = await session.scalar(
        select(func.min(PayoutAttempt.attempt)).where(
            PayoutAttempt.company_id == cid,
            PayoutAttempt.payout_id == payout_id,
            PayoutAttempt.attempt > facts.attempt,
            PayoutAttempt.status == str(AttemptStatus.SETTLED),
        )
    )
    seen = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.company_id == cid,
            AuditEvent.entity_type == "payout",
            AuditEvent.entity_id == payout_id,
            AuditEvent.event == LATE_SETTLEMENT_EVENT,
            AuditEvent.details["attempt"].as_integer() == facts.attempt,
        )
    )
    late = _Late(facts=facts, later_settled_attempt=later, recorded=not seen)
    if late.recorded:
        await AuditRepo.add(
            session, cid, actor_id=WORKER_ACTOR, event=LATE_SETTLEMENT_EVENT,
            entity_type="payout", entity_id=payout_id, details=_late_details(late),
        )  # fmt: skip
    return late


def _late_details(late: _Late) -> dict[str, Any]:
    f = late.facts
    details: dict[str, Any] = {
        "attempt": f.attempt,
        "current_attempt": f.current_attempt,
        "attempt_status": str(f.attempt_status),
        "bank_reference": f.bank_reference,
        "later_attempt_settled": late.later_settled_attempt is not None,
    }
    if f.failure_reason is not None:
        details["failure_reason"] = f.failure_reason
    if late.later_settled_attempt is not None:
        details["later_settled_attempt"] = late.later_settled_attempt
    return details


def _report_late(rt: Runtime, payout_id: UUID, late: _Late) -> None:
    """After commit: the error log every time, the counter once per payout and attempt."""
    log_event(
        _log, logging.ERROR, LATE_SETTLEMENT_EVENT, payout_id=str(payout_id),
        pay_run_id=str(late.facts.pay_run_id), recorded=late.recorded, **_late_details(late),
    )  # fmt: skip
    if late.recorded:
        rt.metrics.late_settlement()


def _after_commit(
    rt: Runtime, payout_id: UUID, body: ResultIn, outcome: ResultOutcome, a: _Applied
) -> None:
    fields: dict[str, Any] = {
        "payout_id": str(payout_id),
        "pay_run_id": str(a.pay_run_id),
        "attempt": body.attempt,
        "outcome": str(outcome),
        "applied": a.applied,
        "attempt_status": str(a.attempt_status),
        "payout_status": str(a.payout_status),
        "run_status": str(a.run.new) if a.run is not None else None,
    }
    if a.failure_reason is not None:
        fields["failure_reason"] = a.failure_reason
    log_event(_log, logging.INFO, "payout.result_recorded", **fields)
    if a.late is not None:
        _report_late(rt, payout_id, a.late)
    if not a.applied:
        return
    rt.metrics.payout_outcome(_OUTCOME_METRIC[a.attempt_status])
    if a.attempt_status is AttemptStatus.INDETERMINATE:
        log_event(
            _log, logging.WARNING, "payout.indeterminate", payout_id=str(payout_id),
            pay_run_id=str(a.pay_run_id), attempt=body.attempt, publish_count=a.publish_count,
        )  # fmt: skip
    if a.attempt_status is AttemptStatus.NEEDS_OPERATOR:
        log_event(
            _log, logging.ERROR, "payout.needs_operator", payout_id=str(payout_id),
            pay_run_id=str(a.pay_run_id), attempt=body.attempt, reason=a.failure_reason,
        )  # fmt: skip
    if a.run is not None:
        report_transition(rt.metrics, a.run)

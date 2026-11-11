"""Record a worker result (design §4.7; PAY-405, PAY-406, PAY-409, R7, C26, C48).

Lock order: idempotency row (1) → run (3) → payout (4) → attempt (5). ``decide_result`` picks
NOT_CURRENT (409, rolled back), STORED (200, unchanged) or APPLY (conditional attempt update,
payout status, run status, audit). The response ``outcome`` is the stored attempt status.
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
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from payroll.api.schemas import ResultIn, ResultOut
from payroll.db.models import Payout, PayoutAttempt
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
    payout_status_for_attempt,
)
from payroll.services import idempotency
from payroll.services.run_status import RunTransition, recompute_run_status, report_transition
from payroll.services.runtime import Runtime

__all__ = ["ConcurrentUpdate", "ResultResponse", "record_result"]

_log: Final = get_logger("payroll.payouts")
POISON_REASON: Final = "poison"
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
class _Applied:
    pay_run_id: UUID
    attempt_status: AttemptStatus
    payout_status: PayoutStatus
    run: RunTransition | None
    applied: bool
    failure_reason: str | None
    publish_count: int


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
    if decision.kind is ResultKind.NOT_CURRENT or attempt is None or current is None:
        log_event(
            _log, logging.INFO, "payout.attempt_not_current", payout_id=str(payout_id),
            attempt=body.attempt, current_attempt=payout.current_attempt, source="result",
        )  # fmt: skip
        raise AttemptNotCurrent(payout.current_attempt)
    if decision.kind is ResultKind.STORED or decision.new_status is None:
        return _Applied(
            pay_run_id=run.id,
            attempt_status=current,
            payout_status=PayoutStatus(payout.status),
            run=None,
            applied=False,
            failure_reason=attempt.failure_reason,
            publish_count=attempt.publish_count,
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
            needs_operator_at=func.now() if new is AttemptStatus.NEEDS_OPERATOR else None,
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
        session, cid, actor_id="service:payout-worker", event="payout.result_recorded",
        entity_type="payout", entity_id=payout_id, details=details,
    )  # fmt: skip
    if new is AttemptStatus.NEEDS_OPERATOR:
        await AuditRepo.add(
            session, cid, actor_id="service:payout-worker", event="payout.needs_operator",
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

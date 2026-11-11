"""Operator resolution of a blocked payout (design §4.4 "Resolve", §3.5a; PAY-502, R8, C13, C48).

One transaction: run ``FOR UPDATE`` (3) → payout ``FOR UPDATE`` (4) → current attempt
``FOR UPDATE`` (5) → inserts. ``check_resolve`` refuses anything but a ``requested`` payout whose
current attempt is ``needs_operator``.

- **redrive:** attempt → ``requested``, reason cleared, ``publish_count = 1``,
  ``last_published_at = now``, ``needs_operator_at`` cleared; ``requested_at`` is kept (it dates
  the bank key); a new outbox row with a fresh ``event_id`` and ``republish_seq = 0``. No wait:
  the bank key is the same, so a redrive can't pay twice.
  Refused for a ``key_window_expired`` attempt (422 ``validation_error``, C73): the worker would
  only GET the same key, get the same 404 and report the same poison.
- **mark_rejected:** only ``PAYOUT_MARK_REJECTED_MIN_WAIT_S`` after ``needs_operator_at`` (else
  409 ``resolve_too_early`` with ``Retry-After``); attempt → ``rejected`` / ``operator_rejected``,
  payout → ``failed``, so the normal retry opens.

Either way the attempt records ``resolved_by`` (the operator's ``sub``). With dual approval on,
that operator can't retry a payout they marked rejected (C73, ``check_retry_dual``).

Either way the run status is recomputed, and ``payout.operator_resolved`` is audited with the
note's **length** only (PAY-NFR-07). "now" is ``Runtime.clock`` (the re-publish task's clock too),
so the C48 wait and the redriven backoff share one time source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from pycommon.auth import Principal
from pycommon.logging import get_logger, log_event
from pycommon.otel import tracer
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from payroll.api.schemas import PayoutOut, ResolveIn
from payroll.db.models import Payout, PayoutAttempt
from payroll.db.repositories import AttemptRepo, AuditRepo, PayoutRepo, PayRunRepo, rowcount
from payroll.db.session import session_scope
from payroll.domain import InvalidFields, PayoutNotBlocked, ResolveTooEarly
from payroll.domain.resolve import (
    ResolveAction,
    check_mark_rejected_wait,
    check_resolve,
    check_resolve_action,
    resolve_transition,
)
from payroll.domain.state import AttemptStatus, PayoutStatus
from payroll.services.common import payout_view
from payroll.services.record_result import ConcurrentUpdate
from payroll.services.run_status import RunTransition, recompute_run_status, report_transition
from payroll.services.runtime import Runtime

__all__ = ["resolve"]

_log: Final = get_logger("payroll.payouts")


@dataclass(frozen=True)
class _Resolved:
    out: PayoutOut
    attempt: int
    event_id: UUID | None
    run: RunTransition


async def resolve(
    rt: Runtime, principal: Principal, run_id: UUID, payout_id: UUID, body: ResolveIn
) -> PayoutOut:
    action = ResolveAction(body.action)
    with tracer("payroll").start_as_current_span("payroll.payout.resolve") as span:
        span.set_attribute("payroll.payout_id", str(payout_id))
        span.set_attribute("payroll.action", str(action))
        try:
            async with session_scope(rt.sessions, "payouts.resolve") as session:
                done = await _resolve_in_tx(
                    rt, session, principal, run_id, payout_id, action=action, body=body
                )
        except (PayoutNotBlocked, ResolveTooEarly, InvalidFields) as exc:
            log_event(
                _log, logging.INFO, "payout.resolve_rejected", payout_id=str(payout_id),
                code=exc.code, attempt_status=getattr(exc, "attempt_status", "needs_operator"),
            )  # fmt: skip
            raise
        span.set_attribute("payroll.attempt", done.attempt)
    if action is ResolveAction.REDRIVE:
        rt.wake_relay()
    else:
        rt.metrics.payout_outcome("operator_rejected")
    fields: dict[str, Any] = {
        "payout_id": str(payout_id),
        "pay_run_id": str(run_id),
        "attempt": done.attempt,
        "action": str(action),
        "note_len": len(body.note),
        "actor_id": principal.sub,
    }
    if done.event_id is not None:
        fields["event_id"] = str(done.event_id)
    log_event(_log, logging.WARNING, "payout.operator_resolved", **fields)
    report_transition(rt.metrics, done.run)
    return done.out


async def _resolve_in_tx(
    rt: Runtime,
    session: AsyncSession,
    principal: Principal,
    run_id: UUID,
    payout_id: UUID,
    *,
    action: ResolveAction,
    body: ResolveIn,
) -> _Resolved:
    cid = principal.cid
    now = rt.clock.now()
    run = await PayRunRepo.get(session, cid, run_id, for_update=True)
    payout = await PayoutRepo.lock_in_run(session, cid, run_id, payout_id)
    attempt = await AttemptRepo.lock(session, cid, payout_id, payout.current_attempt)
    current = AttemptStatus(attempt.status) if attempt is not None else None
    check_resolve(payout_status=PayoutStatus(payout.status), attempt_status=current)
    if attempt is None or attempt.needs_operator_at is None:  # pragma: no cover  (CHECK)
        raise PayoutNotBlocked(str(current) if current is not None else None)
    check_resolve_action(action=action, failure_reason=attempt.failure_reason)
    t = resolve_transition(action)
    event_id: UUID | None = None
    blocked = (PayoutAttempt.id == attempt.id) & (PayoutAttempt.status == "needs_operator")
    if action is ResolveAction.MARK_REJECTED:
        check_mark_rejected_wait(
            needs_operator_at=attempt.needs_operator_at,
            now=now,
            min_wait_s=rt.settings.payout_mark_rejected_min_wait_s,
        )
        result = await session.execute(
            update(PayoutAttempt)
            .where(blocked)
            .values(
                status=str(t.attempt_status),
                failure_reason=t.failure_reason,
                final_at=func.now(),
                needs_operator_at=None,
                resolved_by=principal.sub,
            )
            .execution_options(synchronize_session=False)
        )
        if rowcount(result) != 1:  # pragma: no cover  (the row locks make this unreachable)
            raise ConcurrentUpdate()
        result = await session.execute(
            update(Payout)
            .where(Payout.id == payout_id, Payout.status == "requested")
            .values(status=str(t.payout_status), final_at=func.now())
            .execution_options(synchronize_session=False)
        )
    else:
        result = await session.execute(
            update(PayoutAttempt)
            .where(blocked)
            .values(
                status=str(t.attempt_status),
                failure_reason=t.failure_reason,
                publish_count=t.publish_count,
                last_published_at=now,
                needs_operator_at=None,
                resolved_by=principal.sub,
            )
            .execution_options(synchronize_session=False)
        )
        event_id = await rt.outbox.add_payout_requested(
            session, company_id=cid, payout_id=payout_id, pay_run_id=run_id,
            attempt=attempt.attempt, amount_cents=payout.amount_cents, republish_seq=0,
        )  # fmt: skip
    if rowcount(result) != 1:  # pragma: no cover  (the row locks make this unreachable)
        raise ConcurrentUpdate()
    transition = await recompute_run_status(session, cid, run)
    await AuditRepo.add(
        session, cid, actor_id=principal.sub, event="payout.operator_resolved",
        entity_type="payout", entity_id=payout_id,
        details={"attempt": attempt.attempt, "action": str(action), "note_len": len(body.note)},
    )  # fmt: skip
    out = await payout_view(session, cid, payout_id)
    return _Resolved(out, attempt.attempt, event_id, transition)

"""Re-publish non-final attempts (design §5.3; PAY-407, PAY-409, PAY-NFR-03, R1, R34).

The maintenance task ``republish`` runs this in its single transaction while the runner holds
the ``payroll:maintenance`` advisory lock. Candidates are live (``requested``/``indeterminate``)
current attempts whose backoff ``min(after_s * 2^(publish_count-1), max_backoff_s)`` has passed
and that have no sendable unsent outbox row (a row the relay refused as ``EnvelopeInvalidError``
is never sent, so it does not count). Their payouts are locked in ``id`` order, then the
attempts in ``(payout_id, attempt)`` order (§4.10; not the runs, so result calls for the other
payouts of those runs never wait on a tick), and ``republish_decision`` re-checks each one:

- REPUBLISH: a new outbox row with a fresh ``event_id`` and ``republish_seq = publish_count``
  (before the increment, R34), then ``publish_count + 1`` and ``last_published_at = now``;
- NEEDS_OPERATOR (after ``PAYOUT_REPUBLISH_MAX`` re-publishes): the attempt is blocked with
  ``failure_reason = republish_limit`` and ``needs_operator_at = now`` (C48), audited;
- NEEDS_OPERATOR with ``failure_reason = envelope_invalid`` when the attempt's only unsent rows
  were refused by the relay's envelope check (PAY-NFR-03): otherwise the payout would be stuck
  for good, since retry needs ``failed`` and resolve needs ``needs_operator``.

"now" for the writes is read from the clock **after** the locks are taken, so
``needs_operator_at`` is never earlier than the moment the attempt stops being deliverable
(C48 margin).

The run status doesn't change (a ``needs_operator`` payout stays ``requested``). Metrics and the
domain logs are emitted after commit.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from opentelemetry.trace import Span
from pycommon import Clock
from pycommon.logging import get_logger, log_event
from pycommon.otel import tracer
from sqlalchemy import event, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from payroll.db.models import PayoutAttempt
from payroll.db.repositories import AuditRepo, rowcount
from payroll.domain.retry import RepublishAction, republish_decision
from payroll.domain.state import AttemptStatus
from payroll.events.outbox import OutboxWriter
from payroll.metrics import Metrics
from payroll.settings import Settings

__all__ = ["ENVELOPE_INVALID_REASON", "REPUBLISH_LIMIT_REASON", "republish_due_attempts"]

_log: Final = get_logger("payroll.payouts")
REPUBLISH_LIMIT_REASON: Final = "republish_limit"
ENVELOPE_INVALID_REASON: Final = "envelope_invalid"
# A row the relay refused has outbox.last_error = 'EnvelopeInvalidError' (events/relay.py,
# EnvelopeInvalidError.__name__) and is never sent; the SQL below spells it out.
SYSTEM_ACTOR: Final = "system:maintenance"

_CANDIDATES: Final = text(
    """
    SELECT a.id, a.payout_id
      FROM payroll.payout_attempts a JOIN payroll.payouts p ON p.id = a.payout_id
     WHERE a.status IN ('requested', 'indeterminate')
       AND a.attempt = p.current_attempt
       AND a.last_published_at < CAST(:now AS timestamptz) - make_interval(secs =>
             LEAST(:after_s * power(2, LEAST(a.publish_count - 1, 32)), :max_backoff_s))
       AND NOT EXISTS (SELECT 1 FROM payroll.outbox o
                        WHERE o.payout_id = a.payout_id AND o.attempt = a.attempt
                          AND o.sent_at IS NULL
                          AND o.last_error IS DISTINCT FROM 'EnvelopeInvalidError')
     ORDER BY a.last_published_at
     LIMIT :batch
    """
)
_LOCK_PAYOUTS: Final = text(
    "SELECT id FROM payroll.payouts WHERE id = ANY(CAST(:ids AS uuid[])) ORDER BY id FOR UPDATE"
)
_LOCK_ATTEMPTS: Final = text(
    """
    SELECT a.id, a.payout_id, a.attempt, a.status, a.publish_count, a.last_published_at,
           p.current_attempt, p.amount_cents, p.pay_run_id, p.company_id
      FROM payroll.payout_attempts a JOIN payroll.payouts p ON p.id = a.payout_id
     WHERE a.id = ANY(CAST(:ids AS uuid[]))
     ORDER BY a.payout_id, a.attempt
       FOR UPDATE OF a
    """
)
# Per (payout, attempt) with unsent rows: whether every one of them was refused by the relay.
_UNSENT: Final = text(
    "SELECT payout_id, attempt, bool_and(o.last_error IS NOT DISTINCT FROM "
    "'EnvelopeInvalidError') AS all_invalid FROM payroll.outbox o "
    "WHERE sent_at IS NULL AND payout_id = ANY(CAST(:pids AS uuid[])) GROUP BY 1, 2"
)


@dataclass
class _Outcome:
    republished: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)


def _after_commit(session: AsyncSession, fn: Callable[[], None]) -> None:
    def run(_session: Any) -> None:
        fn()

    event.listen(session.sync_session, "after_commit", run, once=True)


async def republish_due_attempts(
    session: AsyncSession,
    *,
    settings: Settings,
    outbox: OutboxWriter,
    metrics: Metrics,
    clock: Clock,
) -> int:
    """PAY-407: the number of attempts re-published plus those moved to ``needs_operator``."""
    s = settings
    now = clock.now()
    with tracer("payroll").start_as_current_span("payroll.maintenance.republish") as span:
        rows = (
            await session.execute(
                _CANDIDATES,
                {
                    "now": now,
                    "after_s": s.payout_republish_after_s,
                    "max_backoff_s": s.payout_republish_max_backoff_s,
                    "batch": s.payroll_republish_batch,
                },
            )
        ).all()
        if not rows:
            _record_span(span, _Outcome())
            return 0
        await session.execute(_LOCK_PAYOUTS, {"ids": sorted({r.payout_id for r in rows})})
        locked = (await session.execute(_LOCK_ATTEMPTS, {"ids": [r.id for r in rows]})).all()
        unsent: dict[tuple[Any, int], bool] = {
            (pid, att): bool(all_invalid)
            for pid, att, all_invalid in (
                await session.execute(_UNSENT, {"pids": [r.payout_id for r in locked]})
            ).all()
        }
        now = clock.now()  # after the locks: the writes' timestamps (C48 margin)
        out = _Outcome()
        for a in locked:
            all_invalid = unsent.get((a.payout_id, a.attempt))
            await _apply(
                session,
                s,
                outbox,
                now=now,
                a=a,
                has_unsent=all_invalid is False,
                envelope_invalid=all_invalid is True,
                out=out,
            )
        _record_span(span, out)
    _after_commit(session, lambda: _report(metrics, out))
    return len(out.republished) + len(out.blocked)


async def _apply(
    session: AsyncSession,
    s: Settings,
    outbox: OutboxWriter,
    *,
    now: datetime,
    a: Any,
    has_unsent: bool,
    envelope_invalid: bool,
    out: _Outcome,
) -> None:
    action = republish_decision(
        status=AttemptStatus(a.status),
        is_current=a.attempt == a.current_attempt,
        publish_count=a.publish_count,
        last_published_at=a.last_published_at,
        now=now,
        has_unsent_outbox=has_unsent,
        after_s=s.payout_republish_after_s,
        max_backoff_s=s.payout_republish_max_backoff_s,
        max_republish=s.payout_republish_max,
        envelope_invalid=envelope_invalid,
    )
    live = PayoutAttempt.status.in_(["requested", "indeterminate"])
    if action is RepublishAction.REPUBLISH:
        result = await session.execute(
            update(PayoutAttempt)
            .where(PayoutAttempt.id == a.id, PayoutAttempt.publish_count == a.publish_count, live)
            .values(publish_count=a.publish_count + 1, last_published_at=now)
            .execution_options(synchronize_session=False)
        )
        if rowcount(result) == 1:  # the row lock makes 0 unreachable; then nothing is written
            event_id = await outbox.add_payout_requested(
                session, company_id=a.company_id, payout_id=a.payout_id,
                pay_run_id=a.pay_run_id, attempt=a.attempt, amount_cents=a.amount_cents,
                republish_seq=a.publish_count,
            )  # fmt: skip
            out.republished.append(
                {
                    "payout_id": str(a.payout_id),
                    "attempt": a.attempt,
                    "republish_seq": a.publish_count,
                    "event_id": str(event_id),
                    "attempt_status": a.status,
                }
            )
    elif action is RepublishAction.NEEDS_OPERATOR:
        reason = ENVELOPE_INVALID_REASON if envelope_invalid else REPUBLISH_LIMIT_REASON
        result = await session.execute(
            update(PayoutAttempt)
            .where(PayoutAttempt.id == a.id, live)
            .values(
                status="needs_operator",
                failure_reason=reason,
                needs_operator_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if rowcount(result) == 1:
            await AuditRepo.add(
                session, a.company_id, actor_id=SYSTEM_ACTOR, event="payout.needs_operator",
                entity_type="payout", entity_id=a.payout_id,
                details={"attempt": a.attempt, "reason": reason},
            )  # fmt: skip
            out.blocked.append(
                {
                    "payout_id": str(a.payout_id),
                    "pay_run_id": str(a.pay_run_id),
                    "attempt": a.attempt,
                    "reason": reason,
                }
            )


def _record_span(span: Span, out: _Outcome) -> None:
    span.set_attribute("payroll.republished", len(out.republished))
    span.set_attribute("payroll.needs_operator", len(out.blocked))


def _report(metrics: Metrics, out: _Outcome) -> None:
    for fields in out.republished:
        metrics.republished.add(1)
        log_event(_log, logging.INFO, "payout.republished", **fields)
    for fields in out.blocked:
        metrics.payout_outcome("needs_operator")
        log_event(_log, logging.ERROR, "payout.needs_operator", **fields)

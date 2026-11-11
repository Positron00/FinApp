"""``PayrollOutboxSource``: payroll's repository for pycommon's ``RelayLoop`` (design §5.2).

Each claimed row gets a PRODUCER span ``payroll.outbox.publish`` whose parent is the traceparent
stored at write time; the published envelope carries the publish span's own traceparent, so the
worker becomes its child (PAY-NFR-09). ``mark_sent``/``mark_failed`` end the spans.

With ``validate`` on (``PAYROLL_VALIDATE_EVENTS``), the envelope is re-validated against the JSON
Schema after the traceparent rewrite (C60, C63). An invalid row is never published: it is marked
failed (``relay_attempts``, ``last_error``), logged as ``outbox.relay.error`` once and left
unsent, so ``OutboxStuck`` fires, while the rest of the batch still goes out. ``claim_unsent``
skips such rows from then on (C71). Re-publish then moves the
attempt to ``needs_operator`` (``envelope_invalid``), so the payout is never stuck for good
(§5.3, PAY-NFR-03).

``mark_sent`` locks the touched attempt rows in ``(payout_id, attempt)`` order before updating
them, the order every other writer uses (§4.10), so the relay can't deadlock with re-publish.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from pycommon.logging import get_logger, log_event
from pycommon.otel import context_from_traceparent, inject_traceparent, tracer
from pycommon.streams import RelayLoop, StreamItem, StreamProducer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from payroll.events.envelope import EVENT_TYPE, STREAM, EnvelopeInvalidError, validate_envelope

__all__ = ["PayrollOutboxSource", "RelayRef", "build_relay"]

SPAN_NAME: Final = "payroll.outbox.publish"
_log: Final = get_logger("payroll.outbox")

# A row the relay refused (``last_error = 'EnvelopeInvalidError'``) is never claimed again (C71):
# re-validating it on every poll would only log ``outbox.relay.error`` again and take a batch
# slot from a valid row. It stays unsent, so ``outbox_unsent`` and ``OutboxStuck`` still see it,
# and re-publish moves its attempt to ``needs_operator`` (``envelope_invalid``). Rows that failed
# for any other reason (an XADD error) are retried as before.
REFUSED_ERROR: Final = EnvelopeInvalidError.__name__
_CLAIM: Final = text(
    "SELECT id, payload, payout_id, attempt FROM payroll.outbox "
    "WHERE sent_at IS NULL AND last_error IS DISTINCT FROM :refused "
    "ORDER BY created_at, id "
    "LIMIT :limit FOR UPDATE SKIP LOCKED"
)
_MARK_SENT: Final = text("UPDATE payroll.outbox SET sent_at = now() WHERE id = ANY(:ids)")
_LOCK_TOUCHED: Final = text(
    "SELECT a.id FROM payroll.payout_attempts a "
    "JOIN unnest(CAST(:payout_ids AS uuid[]), CAST(:attempts AS smallint[])) AS s(pid, att) "
    "ON a.payout_id = s.pid AND a.attempt = s.att "
    "ORDER BY a.payout_id, a.attempt FOR UPDATE OF a"
)
_TOUCH_ATTEMPTS: Final = text(
    "UPDATE payroll.payout_attempts a SET last_published_at = now(), updated_at = now() "
    "FROM unnest(CAST(:payout_ids AS uuid[]), CAST(:attempts AS smallint[])) AS s(pid, att) "
    "WHERE a.payout_id = s.pid AND a.attempt = s.att AND a.status IN ('requested','indeterminate')"
)
_MARK_FAILED: Final = text(
    "UPDATE payroll.outbox SET relay_attempts = relay_attempts + 1, last_error = :error_type, "
    "updated_at = now() WHERE id = ANY(:ids)"
)


@dataclass(frozen=True)
class RelayRef:
    payout_id: Any
    attempt: int | None
    span: Span


class PayrollOutboxSource:
    """``claim_unsent`` / ``mark_sent`` / ``mark_failed`` over ``payroll.outbox`` (C22)."""

    def __init__(self, *, validate: bool = True) -> None:
        self._validate = validate

    async def claim_unsent(self, session: AsyncSession, *, limit: int) -> Sequence[StreamItem]:
        rows = (await session.execute(_CLAIM, {"limit": limit, "refused": REFUSED_ERROR})).all()
        items: list[StreamItem] = []
        for row in rows:
            payload: dict[str, Any] = dict(row.payload)
            span = tracer("payroll").start_span(
                SPAN_NAME,
                context=context_from_traceparent(payload.get("traceparent")),
                kind=SpanKind.PRODUCER,
                attributes={
                    "messaging.system": "redis",
                    "messaging.destination.name": STREAM,
                    "messaging.message.id": str(row.id),
                    "payroll.payout_id": str(row.payout_id),
                    "payroll.attempt": int(row.attempt or 0),
                },
            )
            with trace.use_span(span, end_on_exit=False):
                publish_tp = inject_traceparent()
            if publish_tp is not None:
                payload["traceparent"] = publish_tp
            item = StreamItem(
                event_id=row.id,
                type=EVENT_TYPE,
                envelope_json=json.dumps(payload, separators=(",", ":")),
                ref=RelayRef(row.payout_id, row.attempt, span),
            )
            if self._validate:
                try:
                    validate_envelope(payload)
                except EnvelopeInvalidError:
                    await self._reject(session, item)
                    continue
            items.append(item)
        return items

    async def _reject(self, session: AsyncSession, item: StreamItem) -> None:
        error_type = REFUSED_ERROR
        await self.mark_failed(session, [item], error_type=error_type)
        log_event(
            _log, logging.ERROR, "outbox.relay.error", error_type=error_type, batch_size=1,
            failed=1, backoff_ms=0,
        )  # fmt: skip

    async def mark_sent(self, session: AsyncSession, items: Sequence[StreamItem]) -> None:
        await session.execute(_MARK_SENT, {"ids": [i.event_id for i in items]})
        refs = [i.ref for i in items if isinstance(i.ref, RelayRef) and i.ref.attempt]
        if refs:
            params = {
                "payout_ids": [r.payout_id for r in refs],
                "attempts": [r.attempt for r in refs],
            }
            await session.execute(_LOCK_TOUCHED, params)
            await session.execute(_TOUCH_ATTEMPTS, params)
        for item in items:
            if isinstance(item.ref, RelayRef):
                item.ref.span.set_status(Status(StatusCode.OK))
                item.ref.span.end()

    async def mark_failed(
        self, session: AsyncSession, items: Sequence[StreamItem], *, error_type: str
    ) -> None:
        await session.execute(
            _MARK_FAILED, {"ids": [i.event_id for i in items], "error_type": error_type}
        )
        for item in items:
            if isinstance(item.ref, RelayRef):
                item.ref.span.set_status(Status(StatusCode.ERROR, error_type))
                item.ref.span.end()


def build_relay(
    sessions: async_sessionmaker[AsyncSession],
    producer: StreamProducer,
    *,
    maxlen: int,
    batch_size: int,
    poll_ms: int,
    validate: bool = True,
) -> RelayLoop:
    return RelayLoop(
        sessions,
        producer,
        PayrollOutboxSource(validate=validate),
        stream=STREAM,
        maxlen=maxlen,
        batch_size=batch_size,
        poll_interval_s=poll_ms / 1000,
    )

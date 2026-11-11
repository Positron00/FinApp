"""``PayrollOutboxSource``: payroll's repository for pycommon's ``RelayLoop`` (design §5.2).

Each claimed row gets a PRODUCER span ``payroll.outbox.publish`` whose parent is the traceparent
stored at write time; the published envelope carries the publish span's own traceparent, so the
worker becomes its child (PAY-NFR-09). ``mark_sent``/``mark_failed`` end the spans.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from pycommon.otel import context_from_traceparent, inject_traceparent, tracer
from pycommon.streams import RelayLoop, StreamItem, StreamProducer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from payroll.events.envelope import EVENT_TYPE, STREAM

__all__ = ["PayrollOutboxSource", "RelayRef", "build_relay"]

SPAN_NAME: Final = "payroll.outbox.publish"

_CLAIM: Final = text(
    "SELECT id, payload, payout_id, attempt FROM payroll.outbox "
    "WHERE sent_at IS NULL ORDER BY created_at, id "
    "LIMIT :limit FOR UPDATE SKIP LOCKED"
)
_MARK_SENT: Final = text("UPDATE payroll.outbox SET sent_at = now() WHERE id = ANY(:ids)")
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

    async def claim_unsent(self, session: AsyncSession, *, limit: int) -> Sequence[StreamItem]:
        rows = (await session.execute(_CLAIM, {"limit": limit})).all()
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
            items.append(
                StreamItem(
                    event_id=row.id,
                    type=EVENT_TYPE,
                    envelope_json=json.dumps(payload, separators=(",", ":")),
                    ref=RelayRef(row.payout_id, row.attempt, span),
                )
            )
        return items

    async def mark_sent(self, session: AsyncSession, items: Sequence[StreamItem]) -> None:
        await session.execute(_MARK_SENT, {"ids": [i.event_id for i in items]})
        refs = [i.ref for i in items if isinstance(i.ref, RelayRef) and i.ref.attempt]
        if refs:
            await session.execute(
                _TOUCH_ATTEMPTS,
                {
                    "payout_ids": [r.payout_id for r in refs],
                    "attempts": [r.attempt for r in refs],
                },
            )
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
) -> RelayLoop:
    return RelayLoop(
        sessions,
        producer,
        PayrollOutboxSource(),
        stream=STREAM,
        maxlen=maxlen,
        batch_size=batch_size,
        poll_interval_s=poll_ms / 1000,
    )

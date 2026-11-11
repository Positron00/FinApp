"""``DisbursementRepo`` (design §3.1.1; PAY-403, PAY-608).

``disbursements`` is a record of tries, not a gate: the upsert resets a ``done`` row to
``submitting`` for a new event. ``processed_events`` is written only by ``finish`` on the normal
path. A pool wait or statement timeout becomes ``WorkerInternalError("db_timeout")``, a lost
connection ``WorkerInternalError("db_unavailable")``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID

from pycommon.db import new_id
from pycommon.errors import db_error_kind
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from payout_worker.db.models import disbursements, processed_events
from payout_worker.errors import WorkerInternalError
from payout_worker.models import Envelope

__all__ = ["PURGE_BATCH", "DisbursementRepo", "DisbursementRepoPort", "purge_processed_events"]

PURGE_BATCH: Final = 1000


class DisbursementRepoPort(Protocol):
    async def is_processed(self, event_id: UUID) -> bool: ...

    async def upsert_submitting(self, env: Envelope) -> None: ...

    async def record_answer(
        self,
        payout_id: UUID,
        attempt: int,
        outcome: str,
        reason: str | None,
        bank_reference: str | None,
    ) -> None: ...

    async def finish(
        self,
        env: Envelope,
        outcome: str,
        reason: str | None = None,
        bank_reference: str | None = None,
    ) -> None: ...


class DisbursementRepo:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, stream: str) -> None:
        self._sessions = sessions
        self._stream = stream

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[AsyncSession]:
        try:
            async with self._sessions() as session, session.begin():
                yield session
        except WorkerInternalError:  # pragma: no cover  (nothing inside raises it)
            raise
        except Exception as exc:
            kind = db_error_kind(exc)
            if kind == "timeout":
                raise WorkerInternalError("db_timeout") from exc
            if kind == "unavailable":
                raise WorkerInternalError("db_unavailable") from exc
            raise

    async def is_processed(self, event_id: UUID) -> bool:
        async with self._tx() as s:
            found = await s.scalar(
                select(processed_events.c.event_id).where(processed_events.c.event_id == event_id)
            )
        return found is not None

    async def upsert_submitting(self, env: Envelope) -> None:
        d = env.data
        stmt = insert(disbursements).values(
            id=new_id(),
            payout_id=d.payout_id,
            attempt=d.attempt,
            company_id=d.company_id,
            status="submitting",
            last_event_id=env.event_id,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="disbursements_payout_attempt_uq",
            set_={
                "status": "submitting",
                "outcome": None,
                "reason": None,
                "last_event_id": stmt.excluded.last_event_id,
                "deliveries": disbursements.c.deliveries + 1,
                "updated_at": func.now(),
            },
        )
        async with self._tx() as s:
            await s.execute(stmt)

    async def record_answer(
        self,
        payout_id: UUID,
        attempt: int,
        outcome: str,
        reason: str | None,
        bank_reference: str | None,
    ) -> None:
        async with self._tx() as s:
            await s.execute(
                update(disbursements)
                .where(disbursements.c.payout_id == payout_id, disbursements.c.attempt == attempt)
                .values(
                    outcome=outcome,
                    reason=reason,
                    bank_reference=func.coalesce(bank_reference, disbursements.c.bank_reference),
                    updated_at=func.now(),
                )
            )

    async def finish(
        self,
        env: Envelope,
        outcome: str,
        reason: str | None = None,
        bank_reference: str | None = None,
    ) -> None:
        """Step 8.1: the disbursement ``done`` and the processed event, in one transaction."""
        d = env.data
        async with self._tx() as s:
            await s.execute(
                update(disbursements)
                .where(
                    disbursements.c.payout_id == d.payout_id, disbursements.c.attempt == d.attempt
                )
                .values(
                    status="done",
                    outcome=outcome,
                    reason=reason,
                    bank_reference=func.coalesce(bank_reference, disbursements.c.bank_reference),
                    updated_at=func.now(),
                )
            )
            await s.execute(
                insert(processed_events)
                .values(
                    event_id=env.event_id,
                    stream=self._stream,
                    payout_id=d.payout_id,
                    attempt=d.attempt,
                    company_id=d.company_id,
                    outcome=outcome,
                )
                .on_conflict_do_nothing(index_elements=[processed_events.c.event_id])
            )


async def purge_processed_events(session: AsyncSession, older_than: datetime) -> int:
    """Delete processed events older than ``older_than`` in batches of 1000 (PAY-608)."""
    total = 0
    while True:
        batch = (
            select(processed_events.c.event_id)
            .where(processed_events.c.processed_at < older_than)
            .limit(PURGE_BATCH)
        )
        result = await session.execute(
            delete(processed_events)
            .where(processed_events.c.event_id.in_(batch))
            .returning(processed_events.c.event_id)
        )
        n = len(result.all())
        total += n
        if n < PURGE_BATCH:
            return total

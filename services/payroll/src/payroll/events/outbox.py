"""``OutboxWriter``: one outbox row per publication, in the caller's transaction (design §5.1)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from pycommon import Clock
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from payroll.db.models import Outbox
from payroll.events.envelope import (
    EVENT_TYPE,
    EVENT_VERSION,
    build_payout_requested,
    validate_envelope,
)

__all__ = ["OutboxWriter"]

_WAKE_FLAG = "payroll.outbox.wake_registered"


class OutboxWriter:
    def __init__(
        self,
        *,
        validate: bool,
        wake: Callable[[], None] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._validate = validate
        self._wake = wake
        self._clock = clock

    def _register_wake(self, session: AsyncSession) -> None:
        wake = self._wake
        if wake is None or session.info.get(_WAKE_FLAG):
            return
        session.info[_WAKE_FLAG] = True
        sync = session.sync_session

        def after_commit(_session: Any) -> None:
            sync.info.pop(_WAKE_FLAG, None)
            wake()

        def after_rollback(_session: Any) -> None:
            sync.info.pop(_WAKE_FLAG, None)

        event.listen(sync, "after_commit", after_commit, once=True)
        event.listen(sync, "after_soft_rollback", after_rollback, once=True)

    async def add_payout_requested(
        self,
        session: AsyncSession,
        *,
        company_id: UUID,
        payout_id: UUID,
        pay_run_id: UUID,
        attempt: int,
        amount_cents: int,
        republish_seq: int,
    ) -> UUID:
        """Write the row (``outbox.id`` = ``event_id``); the local relay wakes after commit."""
        envelope = build_payout_requested(
            company_id=company_id,
            payout_id=payout_id,
            pay_run_id=pay_run_id,
            attempt=attempt,
            amount_cents=amount_cents,
            republish_seq=republish_seq,
            clock=self._clock,
        )
        payload: dict[str, Any] = json.loads(envelope.to_json())
        if self._validate:
            validate_envelope(payload)
        session.add(
            Outbox(
                id=envelope.event_id,
                company_id=company_id,
                event_type=EVENT_TYPE,
                event_version=EVENT_VERSION,
                payload=payload,
                payout_id=payout_id,
                attempt=attempt,
            )
        )
        self._register_wake(session)
        return envelope.event_id

"""The worker's payout instruction (design §4.6; PAY-411, PAY-410).

No locks: one read-committed SELECT. Routing and account come from the attempt's encrypted
snapshot; the response is never logged.
"""

from __future__ import annotations

import json
import logging
from typing import Final
from uuid import UUID

from pycommon.logging import get_logger, log_event
from pycommon.otel import tracer
from sqlalchemy import select

from payroll.api.schemas import InstructionOut
from payroll.crypto import snapshot_aad
from payroll.db.models import Payout, PayoutAttempt
from payroll.db.session import session_scope
from payroll.domain import AttemptNotCurrent, NotFound
from payroll.domain.state import AttemptStatus, instruction_allowed
from payroll.services.common import decrypt_or_500
from payroll.services.runtime import Runtime

__all__ = ["get_instruction"]

_log: Final = get_logger("payroll.payouts")


async def get_instruction(
    rt: Runtime, company_id: UUID, payout_id: UUID, attempt: int
) -> InstructionOut:
    with tracer("payroll").start_as_current_span("payroll.payout.instruction") as span:
        span.set_attribute("payroll.payout_id", str(payout_id))
        span.set_attribute("payroll.attempt", attempt)
        async with session_scope(rt.sessions, "payouts.instruction") as session:
            row = (
                await session.execute(
                    select(Payout, PayoutAttempt)
                    .outerjoin(
                        PayoutAttempt,
                        (PayoutAttempt.payout_id == Payout.id) & (PayoutAttempt.attempt == attempt),
                    )
                    .where(Payout.id == payout_id, Payout.company_id == company_id)
                )
            ).first()
        if row is None:
            raise NotFound("payout_not_found")
        payout, att = row
        status = AttemptStatus(att.status) if att is not None else None
        allowed = instruction_allowed(
            current_attempt=payout.current_attempt, attempt=attempt, attempt_status=status
        )
        span.set_attribute("payroll.allowed", allowed)
        if not allowed or att is None or att.bank_snapshot_enc is None:
            log_event(
                _log, logging.INFO, "payout.attempt_not_current", payout_id=str(payout_id),
                attempt=attempt, current_attempt=payout.current_attempt, source="instruction",
            )  # fmt: skip
            raise AttemptNotCurrent(payout.current_attempt)
        snapshot = json.loads(
            decrypt_or_500(
                rt.keyring,
                att.bank_snapshot_enc,
                aad=snapshot_aad(payout_id, attempt),
                entity="payout_attempt",
                entity_id=att.id,
            )
        )
    log_event(
        _log, logging.DEBUG, "payout.instruction_served", payout_id=str(payout_id), attempt=attempt
    )
    return InstructionOut(
        payout_id=payout.id,
        attempt=attempt,
        amount_cents=payout.amount_cents,
        currency="USD",
        routing=snapshot["routing"],
        account=snapshot["account"],
        requested_at=att.requested_at,
    )

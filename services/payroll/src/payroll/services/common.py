"""Helpers shared by the services: decryption with the PAY-204 failure handling, the run view."""

# ruff: noqa: N818  (the design's error name)

from __future__ import annotations

import logging
from typing import Final, Literal, cast
from uuid import UUID

from pycommon.errors import AppError
from pycommon.logging import get_logger, log_event
from sqlalchemy.ext.asyncio import AsyncSession

from payroll.api.schemas import (
    AttemptOut,
    PayoutCounts,
    PayoutOut,
    PayoutStatusName,
    PayRunOut,
    RunTotalsOut,
)
from payroll.crypto import FieldDecryptError, FieldKeyring
from payroll.db.models import Payout, PayoutAttempt, PayRun
from payroll.db.repositories import AttemptRepo, PayoutRepo, PayRunRepo

__all__ = [
    "DecryptFailed",
    "decrypt_or_500",
    "pay_run_out",
    "pay_run_view",
    "payout_out",
    "payout_view",
]

_log: Final = get_logger("payroll.crypto")


class DecryptFailed(AppError):
    """500 ``internal_error`` after ``crypto.decrypt_failed`` was logged (PAY-204)."""


def decrypt_or_500(
    keyring: FieldKeyring,
    token: str,
    *,
    aad: str,
    entity: Literal["employee", "payout_attempt"],
    entity_id: UUID,
) -> str:
    try:
        return keyring.decrypt(token, aad=aad)
    except FieldDecryptError as exc:
        log_event(
            _log,
            logging.ERROR,
            "crypto.decrypt_failed",
            kid=exc.kid,
            entity=entity,
            entity_id=str(entity_id),
        )
        raise DecryptFailed() from None


def pay_run_out(run: PayRun, counts: dict[str, int]) -> PayRunOut:
    totals = None
    if run.total_net_cents is not None:
        totals = RunTotalsOut(
            gross_cents=run.total_gross_cents or 0,
            tax_cents=run.total_tax_cents or 0,
            retirement_cents=run.total_retirement_cents or 0,
            net_cents=run.total_net_cents,
            funding_required_cents=run.total_net_cents,
        )
    return PayRunOut.model_validate(
        {
            "id": run.id,
            "schedule": run.schedule,
            "period_start": run.period_start,
            "period_end": run.period_end,
            "pay_date": run.pay_date,
            "period_index": run.period_index,
            "periods_per_year": run.periods_per_year,
            "status": run.status,
            "employee_count": run.employee_count,
            "tax_bps": run.tax_bps,
            "retirement_bps": run.retirement_bps,
            "require_dual_approval": run.require_dual_approval,
            "totals": totals,
            "counts": PayoutCounts(**counts),
            "created_by": run.created_by,
            "created_at": run.created_at,
            "calculated_by": run.calculated_by,
            "calculated_by_name": run.calculated_by_name,
            "calculated_at": run.calculated_at,
            "approved_by": run.approved_by,
            "approved_by_name": run.approved_by_name,
            "approved_at": run.approved_at,
            "processing_since": run.processing_since,
            "updated_at": run.updated_at,
        }
    )


async def pay_run_view(session: AsyncSession, company_id: UUID, run_id: UUID) -> PayRunOut:
    """Re-read the run (fresh values) plus its counts."""
    run = await PayRunRepo.get(session, company_id, run_id)
    return pay_run_out(run, await PayRunRepo.counts(session, company_id, run_id))


def payout_out(p: Payout, employee_name: str, attempts: list[PayoutAttempt]) -> PayoutOut:
    """PAY-501, C5: the payout with its attempt history (``account_last4`` only)."""
    return PayoutOut(
        id=p.id,
        pay_run_id=p.pay_run_id,
        payslip_id=p.payslip_id,
        employee_id=p.employee_id,
        employee_name=employee_name,
        amount_cents=p.amount_cents,
        currency="USD",
        status=cast(PayoutStatusName, p.status),
        no_transfer=p.no_transfer,
        current_attempt=p.current_attempt,
        attempts=[AttemptOut.model_validate(a) for a in attempts],
    )


async def payout_view(session: AsyncSession, company_id: UUID, payout_id: UUID) -> PayoutOut:
    """Re-read one payout (fresh values) with its attempts, for the retry and resolve bodies."""
    payout, name = await PayoutRepo.with_name(session, company_id, payout_id)
    attempts = await AttemptRepo.for_payouts(session, company_id, [payout_id])
    return payout_out(payout, name, attempts[payout_id])

"""Pay-run reads (design §4.4 "Read endpoints"; PAY-303, PAY-501, C8, C9): no locks."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from payroll.api.pagination import decode_cursor, encode_cursor
from payroll.api.schemas import (
    AttemptOut,
    Page,
    PayoutOut,
    PayoutStatusName,
    PayRunOut,
    PayslipOut,
)
from payroll.db.repositories import AttemptRepo, PayoutRepo, PayRunRepo, PayslipRepo
from payroll.db.session import session_scope
from payroll.services.common import pay_run_out, pay_run_view
from payroll.services.runtime import Runtime

__all__ = ["get_payslip", "get_run", "list_payouts", "list_payslips", "list_runs"]


def _after(cursor: str | None) -> UUID | None:
    return decode_cursor(cursor) if cursor is not None else None


async def get_run(rt: Runtime, company_id: UUID, run_id: UUID) -> PayRunOut:
    async with session_scope(rt.sessions, "pay_runs.get") as session:
        return await pay_run_view(session, company_id, run_id)


async def list_runs(
    rt: Runtime, company_id: UUID, *, status: str | None, cursor: str | None, limit: int
) -> Page[PayRunOut]:
    after = _after(cursor)
    async with session_scope(rt.sessions, "pay_runs.list") as session:
        runs = await PayRunRepo.page(session, company_id, status=status, after=after, limit=limit)
        items = [
            pay_run_out(r, await PayRunRepo.counts(session, company_id, r.id)) for r in runs[:limit]
        ]
    return Page[PayRunOut](
        items=items, next_cursor=encode_cursor(items[-1].id) if len(runs) > limit else None
    )


async def list_payslips(
    rt: Runtime, company_id: UUID, run_id: UUID, *, cursor: str | None, limit: int
) -> Page[PayslipOut]:
    after = _after(cursor)
    async with session_scope(rt.sessions, "payslips.list") as session:
        await PayRunRepo.get(session, company_id, run_id)
        rows = await PayslipRepo.page(session, company_id, run_id, after=after, limit=limit)
        items = [PayslipOut.model_validate(r) for r in rows[:limit]]
    return Page[PayslipOut](
        items=items, next_cursor=encode_cursor(items[-1].id) if len(rows) > limit else None
    )


async def get_payslip(rt: Runtime, company_id: UUID, run_id: UUID, payslip_id: UUID) -> PayslipOut:
    async with session_scope(rt.sessions, "payslips.get") as session:
        await PayRunRepo.get(session, company_id, run_id)
        return PayslipOut.model_validate(
            await PayslipRepo.get(session, company_id, run_id, payslip_id)
        )


async def list_payouts(
    rt: Runtime,
    company_id: UUID,
    run_id: UUID,
    *,
    status: str | None,
    cursor: str | None,
    limit: int,
) -> Page[PayoutOut]:
    after = _after(cursor)
    async with session_scope(rt.sessions, "payouts.list") as session:
        await PayRunRepo.get(session, company_id, run_id)
        rows = await PayoutRepo.page(
            session, company_id, run_id, status=status, after=after, limit=limit
        )
        page = rows[:limit]
        attempts = await AttemptRepo.for_payouts(session, company_id, [p.id for p, _ in page])
    items = [
        PayoutOut(
            id=p.id,
            pay_run_id=p.pay_run_id,
            payslip_id=p.payslip_id,
            employee_id=p.employee_id,
            employee_name=name,
            amount_cents=p.amount_cents,
            currency="USD",
            status=cast(PayoutStatusName, p.status),
            no_transfer=p.no_transfer,
            current_attempt=p.current_attempt,
            attempts=[AttemptOut.model_validate(a) for a in attempts[p.id]],
        )
        for p, name in page
    ]
    return Page[PayoutOut](
        items=items, next_cursor=encode_cursor(items[-1].id) if len(rows) > limit else None
    )

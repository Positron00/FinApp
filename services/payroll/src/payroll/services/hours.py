"""Enter hours for a pay run (design §4.4 "Hours"; PAY-301, C7).

Lock order (§4.10): the run row ``FOR UPDATE``, then the payslip reads, the hours upsert and, for
a ``calculated`` run, the revert to ``draft``. Calculate and approve take the same run lock, so
hours can never change under a calculation or an approval.
"""

from __future__ import annotations

import logging
from typing import Final
from uuid import UUID

from pycommon.auth import Principal
from pycommon.db import new_id
from pycommon.logging import get_logger, log_event
from pycommon.otel import tracer
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from payroll.api.schemas import HoursEntry, HoursIn, HoursOut
from payroll.db.models import PayRun, PayRunHours, Payslip
from payroll.db.repositories import AuditRepo, PayRunRepo, rowcount
from payroll.db.session import session_scope
from payroll.domain import EmployeeNotInRun, HoursOutOfBounds, NotHourly, PayRunLocked
from payroll.domain.calc import PayType
from payroll.domain.periods import Schedule, max_hours_hundredths
from payroll.domain.state import RunStatus, assert_run_mutable
from payroll.services.runtime import Runtime

__all__ = ["put_hours"]

_log: Final = get_logger("payroll.pay_runs")

# Every field calculate sets on the run, cleared when hours revert it to draft (§4.4 step 4).
_REVERT_RUN: Final = {
    "status": RunStatus.DRAFT.value,
    "calculated_by": None,
    "calculated_by_name": None,
    "calculated_at": None,
    "tax_bps": None,
    "retirement_bps": None,
    "require_dual_approval": None,
    "total_gross_cents": None,
    "total_tax_cents": None,
    "total_retirement_cents": None,
    "total_net_cents": None,
}
_RESET_PAYSLIP: Final = {
    "status": "pending",
    "hours_hundredths": None,
    "gross_cents": None,
    "tax_cents": None,
    "retirement_cents": None,
    "net_cents": None,
}


def _check_entries(
    entries: list[HoursEntry], pay_types: dict[UUID, str], schedule: Schedule
) -> None:
    """``employee_not_in_run``, then ``not_hourly``, then the schedule bound (§4.4 step 2)."""
    missing = [e.employee_id for e in entries if e.employee_id not in pay_types]
    if missing:
        raise EmployeeNotInRun(missing)
    salaried = [e.employee_id for e in entries if pay_types[e.employee_id] != PayType.HOURLY]
    if salaried:
        raise NotHourly(salaried)
    bound = max_hours_hundredths(schedule)
    over = [i for i, e in enumerate(entries) if e.hours_hundredths > bound]
    if over:
        raise HoursOutOfBounds(over)


async def put_hours(rt: Runtime, principal: Principal, run_id: UUID, body: HoursIn) -> HoursOut:
    cid = principal.cid
    with tracer("payroll").start_as_current_span("payroll.pay_run.hours") as span:
        span.set_attribute("payroll.pay_run_id", str(run_id))
        span.set_attribute("payroll.entries", len(body.entries))
        async with session_scope(rt.sessions, "pay_runs.hours") as session:
            run = await PayRunRepo.get(session, cid, run_id, for_update=True)
            status = RunStatus(run.status)
            assert_run_mutable(status)
            slips = await session.execute(
                select(Payslip.employee_id, Payslip.pay_type).where(
                    Payslip.pay_run_id == run_id, Payslip.company_id == cid
                )
            )
            pay_types: dict[UUID, str] = {row.employee_id: row.pay_type for row in slips}
            _check_entries(body.entries, pay_types, Schedule(run.schedule))
            stmt = insert(PayRunHours).values(
                [
                    {
                        "id": new_id(),
                        "company_id": cid,
                        "pay_run_id": run_id,
                        "employee_id": e.employee_id,
                        "hours_hundredths": e.hours_hundredths,
                        "entered_by": principal.sub,
                    }
                    for e in sorted(body.entries, key=lambda e: e.employee_id)
                ]
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_pay_run_hours",
                    set_={
                        "hours_hundredths": stmt.excluded.hours_hundredths,
                        "entered_by": stmt.excluded.entered_by,
                        "updated_at": func.now(),
                    },
                )
            )
            reverted = status is RunStatus.CALCULATED
            if reverted:
                result = await session.execute(
                    update(PayRun)
                    .where(PayRun.id == run_id, PayRun.status == RunStatus.CALCULATED.value)
                    .values(**_REVERT_RUN)
                    .execution_options(synchronize_session=False)
                )
                if rowcount(result) != 1:  # pragma: no cover  (the row lock makes this unreachable)
                    raise PayRunLocked(run.status)
                await session.execute(
                    update(Payslip)
                    .where(Payslip.pay_run_id == run_id, Payslip.company_id == cid)
                    .values(**_RESET_PAYSLIP)
                    .execution_options(synchronize_session=False)
                )
            await AuditRepo.add(
                session,
                cid,
                actor_id=principal.sub,
                event="payrun.hours_updated",
                entity_type="pay_run",
                entity_id=run_id,
                details={"entries": len(body.entries), "reverted_to_draft": reverted},
            )
            stored = (
                await session.execute(
                    select(PayRunHours.employee_id, PayRunHours.hours_hundredths)
                    .where(PayRunHours.pay_run_id == run_id, PayRunHours.company_id == cid)
                    .order_by(PayRunHours.employee_id)
                )
            ).all()
        span.set_attribute("payroll.reverted_to_draft", reverted)
    log_event(
        _log,
        logging.INFO,
        "payrun.hours_updated",
        pay_run_id=str(run_id),
        entries=len(body.entries),
        reverted_to_draft=reverted,
    )
    return HoursOut(
        pay_run_id=run_id,
        status="draft",
        entries=[HoursEntry(employee_id=e, hours_hundredths=h) for e, h in stored],
    )

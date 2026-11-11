"""Calculate a pay run (design §4.4 "Calculate"; PAY-302, ADR 0006).

Recalculating a ``calculated`` run is allowed and recomputes it. Totals are the sums of the
amounts being stored.
"""

from __future__ import annotations

import logging
import time
from typing import Final
from uuid import UUID

from pycommon.auth import Principal
from pycommon.logging import get_logger, log_event
from pycommon.otel import tracer
from sqlalchemy import func, select, update

from payroll.api.schemas import PayRunOut
from payroll.db.models import PayRun, PayRunHours, Payslip
from payroll.db.repositories import AuditRepo, PayRunRepo, SettingsRepo, rowcount
from payroll.db.session import session_scope
from payroll.domain import PayRunLocked
from payroll.domain.calc import PayslipInput, PayType, calculate_run, run_totals
from payroll.domain.state import MUTABLE_RUN, RunStatus, assert_run_mutable
from payroll.services.common import pay_run_view
from payroll.services.runtime import Runtime

__all__ = ["calculate"]

_log: Final = get_logger("payroll.pay_runs")


async def calculate(rt: Runtime, principal: Principal, run_id: UUID) -> PayRunOut:
    cid = principal.cid
    started = time.monotonic()
    with tracer("payroll").start_as_current_span("payroll.pay_run.calculate") as span:
        span.set_attribute("payroll.pay_run_id", str(run_id))
        async with session_scope(rt.sessions, "pay_runs.calculate") as session:
            run = await PayRunRepo.get(session, cid, run_id, for_update=True)
            assert_run_mutable(RunStatus(run.status))
            rows = (
                await session.execute(
                    select(Payslip, PayRunHours.hours_hundredths)
                    .outerjoin(
                        PayRunHours,
                        (PayRunHours.pay_run_id == Payslip.pay_run_id)
                        & (PayRunHours.employee_id == Payslip.employee_id),
                    )
                    .where(Payslip.pay_run_id == run_id, Payslip.company_id == cid)
                    .order_by(Payslip.id)
                )
            ).all()
            settings = await SettingsRepo.get(session, cid)
            inputs = [
                PayslipInput(
                    payslip_id=p.id,
                    employee_id=p.employee_id,
                    pay_type=PayType(p.pay_type),
                    annual_salary_cents=p.annual_salary_cents,
                    hourly_rate_cents=p.hourly_rate_cents,
                    hours_hundredths=hours if p.pay_type == PayType.HOURLY else None,
                )
                for p, hours in rows
            ]
            amounts = calculate_run(
                inputs,
                period_index=run.period_index,
                periods_per_year=run.periods_per_year,
                tax_bps=settings.tax_rate_bps,
                retirement_bps=settings.retirement_bps,
            )
            await session.execute(
                update(Payslip),
                [
                    {
                        "id": i.payslip_id,
                        "status": "calculated",
                        "hours_hundredths": i.hours_hundredths,
                        "gross_cents": amounts[i.payslip_id].gross_cents,
                        "tax_cents": amounts[i.payslip_id].tax_cents,
                        "retirement_cents": amounts[i.payslip_id].retirement_cents,
                        "net_cents": amounts[i.payslip_id].net_cents,
                    }
                    for i in inputs
                ],
            )
            totals = run_totals(amounts.values())
            result = await session.execute(
                update(PayRun)
                .where(
                    PayRun.id == run_id,
                    PayRun.status.in_([str(s) for s in MUTABLE_RUN]),
                )
                .values(
                    status="calculated",
                    tax_bps=settings.tax_rate_bps,
                    retirement_bps=settings.retirement_bps,
                    require_dual_approval=settings.require_dual_approval,
                    total_gross_cents=totals.gross_cents,
                    total_tax_cents=totals.tax_cents,
                    total_retirement_cents=totals.retirement_cents,
                    total_net_cents=totals.net_cents,
                    calculated_by=principal.sub,
                    calculated_by_name=principal.name,
                    calculated_at=func.now(),
                )
                .execution_options(synchronize_session=False)
            )
            if rowcount(result) != 1:  # pragma: no cover  (the row lock makes this unreachable)
                raise PayRunLocked(run.status)
            await AuditRepo.add(
                session,
                cid,
                actor_id=principal.sub,
                event="payrun.calculated",
                entity_type="pay_run",
                entity_id=run_id,
                details={
                    "tax_bps": settings.tax_rate_bps,
                    "retirement_bps": settings.retirement_bps,
                    "require_dual_approval": settings.require_dual_approval,
                    "payslips": len(inputs),
                },
            )
            out = await pay_run_view(session, cid, run_id)
        span.set_attribute("payroll.payslips", len(inputs))
    rt.metrics.run_outcome("calculated")
    log_event(
        _log,
        logging.INFO,
        "payrun.calculated",
        pay_run_id=str(run_id),
        payslips=len(inputs),
        tax_bps=settings.tax_rate_bps,
        retirement_bps=settings.retirement_bps,
        require_dual_approval=settings.require_dual_approval,
        duration_ms=round((time.monotonic() - started) * 1000, 1),
        actor_id=principal.sub,
    )
    return out

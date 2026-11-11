"""Create a pay run (design §4.4 "Create"; PAY-300, R33).

Lock order (§4.10): the per-company advisory lock, then the settings row ``FOR SHARE`` (so the
biweekly anchor can't change underneath), then plain reads and the inserts. The employee overlap
check (``employee_in_overlapping_run``) arrives with F3.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Final

from pycommon.auth import Principal
from pycommon.db import new_id
from pycommon.logging import get_logger, log_event
from pycommon.otel import tracer
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from payroll.api.schemas import PayRunCreate, PayRunOut
from payroll.db.models import PayRun, Payslip
from payroll.db.repositories import AuditRepo, EmployeeRepo, PayRunRepo, SettingsRepo
from payroll.db.session import session_scope
from payroll.domain import DomainError, NoEmployees, PayRunExists
from payroll.domain.periods import Schedule, derive_period
from payroll.services.common import pay_run_view
from payroll.services.runtime import Runtime

__all__ = ["create_run"]

_log: Final = get_logger("payroll.pay_runs")
RUN_INDEX: Final = "uq_pay_runs_company_schedule_period"
_ADVISORY: Final = text("SELECT pg_advisory_xact_lock(hashtext('payroll.create_run:' || :cid))")


def _rejected(code: str, schedule: str, period_start: date) -> None:
    log_event(
        _log,
        logging.INFO,
        "payrun.create_rejected",
        code=code,
        schedule=schedule,
        period_start=period_start.isoformat(),
    )


async def create_run(rt: Runtime, principal: Principal, body: PayRunCreate) -> PayRunOut:
    cid = principal.cid
    with tracer("payroll").start_as_current_span("payroll.pay_run.create") as span:
        try:
            async with session_scope(rt.sessions, "pay_runs.create") as session:
                await session.execute(_ADVISORY, {"cid": str(cid)})
                settings = await SettingsRepo.get(session, cid, lock="share")
                period = derive_period(
                    Schedule(body.schedule), body.period_start, settings.biweekly_anchor
                )
                existing = await PayRunRepo.find_live(
                    session, cid, body.schedule, period.period_start
                )
                if existing is not None:
                    raise PayRunExists(existing)
                employees = await EmployeeRepo.active_on_schedule(session, cid, body.schedule)
                if not employees:
                    raise NoEmployees()
                run_id = new_id()
                session.add(
                    PayRun(
                        id=run_id,
                        company_id=cid,
                        schedule=body.schedule,
                        period_start=period.period_start,
                        period_end=period.period_end,
                        pay_date=period.pay_date,
                        period_index=period.period_index,
                        periods_per_year=period.periods_per_year,
                        status="draft",
                        employee_count=len(employees),
                        created_by=principal.sub,
                    )
                )
                try:
                    await session.flush()
                except IntegrityError as exc:
                    if RUN_INDEX in str(exc.orig):
                        raise PayRunExists(None) from None
                    raise
                await session.execute(
                    insert(Payslip),
                    [
                        {
                            "id": new_id(),
                            "company_id": cid,
                            "pay_run_id": run_id,
                            "employee_id": e.id,
                            "employee_name": e.name,
                            "pay_type": e.pay_type,
                            "annual_salary_cents": e.annual_salary_cents,
                            "hourly_rate_cents": e.hourly_rate_cents,
                            "status": "pending",
                        }
                        for e in employees
                    ],
                )
                await AuditRepo.add(
                    session,
                    cid,
                    actor_id=principal.sub,
                    event="payrun.created",
                    entity_type="pay_run",
                    entity_id=run_id,
                    details={
                        "schedule": body.schedule,
                        "period_start": period.period_start.isoformat(),
                        "employee_count": len(employees),
                    },
                )
                out = await pay_run_view(session, cid, run_id)
        except DomainError as exc:
            if isinstance(exc, (PayRunExists, NoEmployees)) or exc.code == "invalid_period_start":
                _rejected(exc.error_code, body.schedule, body.period_start)
            raise
        span.set_attribute("payroll.pay_run_id", str(run_id))
        span.set_attribute("payroll.schedule", body.schedule)
        span.set_attribute("payroll.employee_count", len(employees))
    log_event(
        _log,
        logging.INFO,
        "payrun.created",
        pay_run_id=str(run_id),
        schedule=body.schedule,
        period_start=period.period_start.isoformat(),
        employee_count=len(employees),
        actor_id=principal.sub,
    )
    return out

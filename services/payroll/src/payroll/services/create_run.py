"""Create and delete pay runs (design §4.4 "Create" and "Delete"; PAY-300, PAY-304, R33).

Lock order (§4.10): the per-company advisory lock, then the settings row ``FOR SHARE`` (so the
biweekly anchor can't change underneath), then plain reads and the inserts. Every candidate
employee is checked for coverage by another non-deleted run whose days overlap the new period
(``employee_in_overlapping_run``), so no one is paid twice for the same days (C63).

Delete is a soft delete of a ``draft`` or ``calculated`` run under the run lock: ``deleted_at``
frees the period (the unique index and the overlap check ignore deleted runs); payslips and hours
stay for the record.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Final
from uuid import UUID

from pycommon.auth import Principal
from pycommon.db import new_id
from pycommon.logging import get_logger, log_event
from pycommon.otel import tracer
from sqlalchemy import func, insert, text, update
from sqlalchemy.exc import IntegrityError

from payroll.api.schemas import PayRunCreate, PayRunOut
from payroll.db.models import PayRun, Payslip
from payroll.db.repositories import (
    AuditRepo,
    EmployeeRepo,
    PayRunRepo,
    PayslipRepo,
    SettingsRepo,
    rowcount,
)
from payroll.db.session import session_scope
from payroll.domain import (
    DomainError,
    EmployeeInOverlappingRun,
    NoEmployees,
    PayRunExists,
    PayRunLocked,
)
from payroll.domain.periods import Coverage, Schedule, derive_period, find_overlapping_employees
from payroll.domain.state import MUTABLE_RUN, RunStatus, assert_run_mutable
from payroll.services.common import pay_run_view
from payroll.services.runtime import Runtime

__all__ = ["create_run", "delete_run"]

_log: Final = get_logger("payroll.pay_runs")
RUN_INDEX: Final = "uq_pay_runs_company_schedule_period"
_ADVISORY: Final = text("SELECT pg_advisory_xact_lock(hashtext('payroll.create_run:' || :cid))")


def _rejected(
    code: str, schedule: str, period_start: date, *, employee_count: int | None = None
) -> None:
    fields: dict[str, Any] = {
        "code": code,
        "schedule": schedule,
        "period_start": period_start.isoformat(),
    }
    if employee_count is not None:
        fields["employee_count"] = employee_count
    log_event(_log, logging.INFO, "payrun.create_rejected", **fields)


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
                ids = [e.id for e in employees]
                rows = await PayslipRepo.coverage(
                    session, cid, ids, period.period_start, period.period_end
                )
                overlapping = find_overlapping_employees(
                    ids,
                    (Coverage(e, s, t) for e, s, t in rows),
                    period.period_start,
                    period.period_end,
                )
                if overlapping:
                    raise EmployeeInOverlappingRun(overlapping)
                run_id = new_id()
                run = PayRun(
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
                try:
                    # Added inside a savepoint, so a unique-index race rolls back only the
                    # insert and the transaction can still re-read the winner.
                    async with session.begin_nested():
                        session.add(run)
                        await session.flush()
                except IntegrityError as exc:
                    if RUN_INDEX not in str(exc.orig):
                        raise
                    # The unique index waited for the other create to commit; READ COMMITTED
                    # sees its row now, so the 409 carries the real ID (§3.7, C63).
                    winner = await PayRunRepo.find_live(
                        session, cid, body.schedule, period.period_start
                    )
                    raise PayRunExists(winner) from None
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
            if isinstance(exc, EmployeeInOverlappingRun):
                _rejected(
                    exc.error_code, body.schedule, body.period_start,
                    employee_count=len(exc.employee_ids),
                )  # fmt: skip
            elif isinstance(exc, (PayRunExists, NoEmployees)) or exc.code == "invalid_period_start":
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


async def delete_run(rt: Runtime, principal: Principal, run_id: UUID) -> None:
    """PAY-304: soft-delete a ``draft`` or ``calculated`` run; 409 ``pay_run_locked`` otherwise."""
    cid = principal.cid
    async with session_scope(rt.sessions, "pay_runs.delete") as session:
        run = await PayRunRepo.get(session, cid, run_id, for_update=True)
        status = RunStatus(run.status)
        assert_run_mutable(status)
        result = await session.execute(
            update(PayRun)
            .where(
                PayRun.id == run_id,
                PayRun.company_id == cid,
                PayRun.status.in_([str(s) for s in MUTABLE_RUN]),
                PayRun.deleted_at.is_(None),
            )
            .values(deleted_at=func.now(), deleted_by=principal.sub)
            .execution_options(synchronize_session=False)
        )
        if rowcount(result) != 1:  # pragma: no cover  (the row lock makes this unreachable)
            raise PayRunLocked(run.status)
        await AuditRepo.add(
            session,
            cid,
            actor_id=principal.sub,
            event="payrun.deleted",
            entity_type="pay_run",
            entity_id=run_id,
            details={"status_at_delete": str(status)},
        )
    log_event(
        _log,
        logging.INFO,
        "payrun.deleted",
        pay_run_id=str(run_id),
        status_at_delete=str(status),
        actor_id=principal.sub,
    )

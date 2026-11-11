"""Repositories (design §1). Every method takes ``company_id`` first and binds every value.

Keyset pagination (§4.9): the sort key of the cursor's ``after`` row is resolved in the same
company and parent scope (an unknown ``after`` is 422 ``invalid_cursor``), then the page is
``(sort cols) > (that key)`` (``<`` for descending orders), ``limit + 1`` rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from opentelemetry import trace
from pycommon.db import new_id
from pycommon.logging import request_id_var
from sqlalchemy import ColumnElement, CursorResult, Executable, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from payroll.db.models import (
    AuditEvent,
    CompanySettings,
    Employee,
    Payout,
    PayoutAttempt,
    PayRun,
    Payslip,
)
from payroll.domain import InvalidCursor, NotFound

__all__ = [
    "AttemptRepo",
    "AuditRepo",
    "EmployeeRepo",
    "PayRunRepo",
    "PayoutRepo",
    "PayslipRepo",
    "SettingsRepo",
    "rowcount",
]


def rowcount(result: Any) -> int:
    return int(cast(CursorResult[Any], result).rowcount)


async def _anchor(session: AsyncSession, stmt: Executable) -> tuple[Any, ...]:
    row = (await session.execute(stmt)).first()
    if row is None:
        raise InvalidCursor()
    return tuple(row)


def _after(
    cols: Sequence[Any], values: tuple[Any, ...], *, descending: bool
) -> ColumnElement[bool]:
    key, anchor = tuple_(*cols), tuple_(*values)
    return key < anchor if descending else key > anchor


class SettingsRepo:
    @staticmethod
    async def get(
        session: AsyncSession, company_id: UUID, *, lock: str | None = None
    ) -> CompanySettings:
        stmt = select(CompanySettings).where(CompanySettings.company_id == company_id)
        if lock == "update":
            stmt = stmt.with_for_update()
        elif lock == "share":
            stmt = stmt.with_for_update(read=True)
        row = (await session.scalars(stmt.execution_options(populate_existing=True))).first()
        if row is None:
            raise NotFound("company_settings_not_found")
        return row

    @staticmethod
    async def biweekly_run_exists(session: AsyncSession, company_id: UUID) -> bool:
        inner = select(PayRun.id).where(
            PayRun.company_id == company_id,
            PayRun.schedule == "biweekly",
            PayRun.deleted_at.is_(None),
        )
        return bool((await session.execute(select(inner.exists()))).scalar_one())


class EmployeeRepo:
    @staticmethod
    async def get(session: AsyncSession, company_id: UUID, employee_id: UUID) -> Employee:
        stmt = select(Employee).where(Employee.id == employee_id, Employee.company_id == company_id)
        row = (await session.scalars(stmt)).first()
        if row is None:
            raise NotFound("employee_not_found")
        return row

    @staticmethod
    async def lock(session: AsyncSession, company_id: UUID, employee_id: UUID) -> Employee:
        """``FOR UPDATE`` (lock order 6, §4.10): the PATCH never locks ``pay_runs``."""
        stmt = (
            select(Employee)
            .where(Employee.id == employee_id, Employee.company_id == company_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        row = (await session.scalars(stmt)).first()
        if row is None:
            raise NotFound("employee_not_found")
        return row

    @staticmethod
    async def page(
        session: AsyncSession,
        company_id: UUID,
        *,
        status: str | None,
        after: UUID | None,
        limit: int,
    ) -> list[Employee]:
        stmt = select(Employee).where(Employee.company_id == company_id)
        if status is not None:
            stmt = stmt.where(Employee.status == status)
        if after is not None:
            key = await _anchor(
                session,
                select(Employee.name, Employee.id).where(
                    Employee.id == after, Employee.company_id == company_id
                ),
            )
            stmt = stmt.where(_after((Employee.name, Employee.id), key, descending=False))
        stmt = stmt.order_by(Employee.name, Employee.id).limit(limit + 1)
        return list((await session.scalars(stmt)).all())

    @staticmethod
    async def active_on_schedule(
        session: AsyncSession, company_id: UUID, schedule: str
    ) -> list[Employee]:
        stmt = (
            select(Employee)
            .where(
                Employee.company_id == company_id,
                Employee.schedule == schedule,
                Employee.status == "active",
            )
            .order_by(Employee.id)
        )
        return list((await session.scalars(stmt)).all())

    @staticmethod
    async def for_share(
        session: AsyncSession, company_id: UUID, ids: Sequence[UUID]
    ) -> dict[UUID, Employee]:
        """``FOR SHARE`` in ``id`` order (lock order 6, §4.10)."""
        stmt = (
            select(Employee)
            .where(Employee.id.in_(list(ids)), Employee.company_id == company_id)
            .order_by(Employee.id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        return {e.id: e for e in (await session.scalars(stmt)).all()}


class PayRunRepo:
    @staticmethod
    async def get(
        session: AsyncSession, company_id: UUID, run_id: UUID, *, for_update: bool = False
    ) -> PayRun:
        stmt = select(PayRun).where(
            PayRun.id == run_id, PayRun.company_id == company_id, PayRun.deleted_at.is_(None)
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = (await session.scalars(stmt.execution_options(populate_existing=True))).first()
        if row is None:
            raise NotFound("pay_run_not_found")
        return row

    @staticmethod
    async def find_live(
        session: AsyncSession, company_id: UUID, schedule: str, period_start: Any
    ) -> UUID | None:
        stmt = select(PayRun.id).where(
            PayRun.company_id == company_id,
            PayRun.schedule == schedule,
            PayRun.period_start == period_start,
            PayRun.deleted_at.is_(None),
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def page(
        session: AsyncSession,
        company_id: UUID,
        *,
        status: str | None,
        after: UUID | None,
        limit: int,
    ) -> list[PayRun]:
        stmt = select(PayRun).where(PayRun.company_id == company_id, PayRun.deleted_at.is_(None))
        if status is not None:
            stmt = stmt.where(PayRun.status == status)
        if after is not None:
            key = await _anchor(
                session,
                select(PayRun.period_start, PayRun.id).where(
                    PayRun.id == after,
                    PayRun.company_id == company_id,
                    PayRun.deleted_at.is_(None),
                ),
            )
            stmt = stmt.where(_after((PayRun.period_start, PayRun.id), key, descending=True))
        stmt = stmt.order_by(PayRun.period_start.desc(), PayRun.id.desc()).limit(limit + 1)
        return list((await session.scalars(stmt)).all())

    @staticmethod
    async def counts(session: AsyncSession, company_id: UUID, run_id: UUID) -> dict[str, int]:
        """The six C9 counts from one grouped query (design §4.4 "Read endpoints")."""
        stmt = (
            select(Payout.status, PayoutAttempt.status, Payout.no_transfer, func.count())
            .select_from(Payout)
            .outerjoin(
                PayoutAttempt,
                (PayoutAttempt.payout_id == Payout.id)
                & (PayoutAttempt.attempt == Payout.current_attempt),
            )
            .where(Payout.pay_run_id == run_id, Payout.company_id == company_id)
            .group_by(Payout.status, PayoutAttempt.status, Payout.no_transfer)
        )
        out = dict.fromkeys(
            ("requested", "awaiting_bank", "blocked", "paid", "no_transfer", "failed"), 0
        )
        for payout_status, attempt_status, no_transfer, n in (await session.execute(stmt)).all():
            out[payout_status] += n
            if payout_status == "requested" and attempt_status == "indeterminate":
                out["awaiting_bank"] += n
            if payout_status == "requested" and attempt_status == "needs_operator":
                out["blocked"] += n
            if no_transfer:
                out["no_transfer"] += n
        return out


class PayslipRepo:
    @staticmethod
    async def coverage(
        session: AsyncSession,
        company_id: UUID,
        employee_ids: Sequence[UUID],
        new_start: Any,
        new_end: Any,
    ) -> list[tuple[UUID, Any, Any]]:
        """``(employee_id, period_start, period_end)`` of non-deleted runs overlapping the range
        (design §4.4 "Create" step 5; PAY-300)."""
        if not employee_ids:
            return []
        stmt = (
            select(Payslip.employee_id, PayRun.period_start, PayRun.period_end)
            .join(PayRun, PayRun.id == Payslip.pay_run_id)
            .where(
                PayRun.company_id == company_id,
                PayRun.deleted_at.is_(None),
                Payslip.employee_id.in_(list(employee_ids)),
                PayRun.period_start <= new_end,
                PayRun.period_end >= new_start,
            )
        )
        return [(e, s, t) for e, s, t in (await session.execute(stmt)).all()]

    @staticmethod
    async def for_run(session: AsyncSession, company_id: UUID, run_id: UUID) -> list[Payslip]:
        stmt = (
            select(Payslip)
            .where(Payslip.pay_run_id == run_id, Payslip.company_id == company_id)
            .order_by(Payslip.id)
            .execution_options(populate_existing=True)
        )
        return list((await session.scalars(stmt)).all())

    @staticmethod
    async def get(
        session: AsyncSession, company_id: UUID, run_id: UUID, payslip_id: UUID
    ) -> Payslip:
        stmt = select(Payslip).where(
            Payslip.id == payslip_id,
            Payslip.pay_run_id == run_id,
            Payslip.company_id == company_id,
        )
        row = (await session.scalars(stmt)).first()
        if row is None:
            raise NotFound("payslip_not_found")
        return row

    @staticmethod
    async def page(
        session: AsyncSession, company_id: UUID, run_id: UUID, *, after: UUID | None, limit: int
    ) -> list[Payslip]:
        scope = (Payslip.pay_run_id == run_id) & (Payslip.company_id == company_id)
        stmt = select(Payslip).where(scope)
        if after is not None:
            key = await _anchor(
                session,
                select(Payslip.employee_name, Payslip.id).where(scope, Payslip.id == after),
            )
            stmt = stmt.where(_after((Payslip.employee_name, Payslip.id), key, descending=False))
        stmt = stmt.order_by(Payslip.employee_name, Payslip.id).limit(limit + 1)
        return list((await session.scalars(stmt)).all())


class PayoutRepo:
    @staticmethod
    async def get(session: AsyncSession, company_id: UUID, payout_id: UUID) -> Payout:
        stmt = select(Payout).where(Payout.id == payout_id, Payout.company_id == company_id)
        row = (await session.scalars(stmt)).first()
        if row is None:
            raise NotFound("payout_not_found")
        return row

    @staticmethod
    async def lock(session: AsyncSession, company_id: UUID, payout_id: UUID) -> Payout:
        stmt = (
            select(Payout)
            .where(Payout.id == payout_id, Payout.company_id == company_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        row = (await session.scalars(stmt)).first()
        if row is None:  # pragma: no cover  (payouts are never deleted)
            raise NotFound("payout_not_found")
        return row

    @staticmethod
    async def lock_in_run(
        session: AsyncSession, company_id: UUID, run_id: UUID, payout_id: UUID
    ) -> Payout:
        """``FOR UPDATE`` (lock order 4); a payout of another run or company is 404."""
        stmt = (
            select(Payout)
            .where(
                Payout.id == payout_id,
                Payout.pay_run_id == run_id,
                Payout.company_id == company_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        row = (await session.scalars(stmt)).first()
        if row is None:
            raise NotFound("payout_not_found")
        return row

    @staticmethod
    async def with_name(
        session: AsyncSession, company_id: UUID, payout_id: UUID
    ) -> tuple[Payout, str]:
        """The payout (fresh values) and its payslip's ``employee_name``."""
        stmt = (
            select(Payout, Payslip.employee_name)
            .join(Payslip, Payslip.id == Payout.payslip_id)
            .where(Payout.id == payout_id, Payout.company_id == company_id)
            .execution_options(populate_existing=True)
        )
        row = (await session.execute(stmt)).first()
        if row is None:  # pragma: no cover  (callers hold the payout)
            raise NotFound("payout_not_found")
        return row[0], row[1]

    @staticmethod
    async def statuses(session: AsyncSession, company_id: UUID, run_id: UUID) -> list[str]:
        stmt = select(Payout.status).where(
            Payout.pay_run_id == run_id, Payout.company_id == company_id
        )
        return list((await session.scalars(stmt)).all())

    @staticmethod
    async def page(
        session: AsyncSession,
        company_id: UUID,
        run_id: UUID,
        *,
        status: str | None,
        after: UUID | None,
        limit: int,
    ) -> list[tuple[Payout, str]]:
        """Payouts with the payslip's ``employee_name``, ordered by name then ID (C8)."""
        scope = (Payout.pay_run_id == run_id) & (Payout.company_id == company_id)
        stmt = (
            select(Payout, Payslip.employee_name)
            .join(Payslip, Payslip.id == Payout.payslip_id)
            .where(scope)
        )
        if status is not None:
            stmt = stmt.where(Payout.status == status)
        if after is not None:
            key = await _anchor(
                session,
                select(Payslip.employee_name, Payout.id)
                .join(Payslip, Payslip.id == Payout.payslip_id)
                .where(scope, Payout.id == after),
            )
            stmt = stmt.where(_after((Payslip.employee_name, Payout.id), key, descending=False))
        stmt = stmt.order_by(Payslip.employee_name, Payout.id).limit(limit + 1)
        return [(p, name) for p, name in (await session.execute(stmt)).all()]


class AttemptRepo:
    @staticmethod
    async def lock(
        session: AsyncSession, company_id: UUID, payout_id: UUID, attempt: int
    ) -> PayoutAttempt | None:
        stmt = (
            select(PayoutAttempt)
            .where(
                PayoutAttempt.payout_id == payout_id,
                PayoutAttempt.attempt == attempt,
                PayoutAttempt.company_id == company_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await session.scalars(stmt)).first()

    @staticmethod
    async def for_payouts(
        session: AsyncSession, company_id: UUID, payout_ids: Sequence[UUID]
    ) -> dict[UUID, list[PayoutAttempt]]:
        out: dict[UUID, list[PayoutAttempt]] = {pid: [] for pid in payout_ids}
        if not payout_ids:
            return out
        stmt = (
            select(PayoutAttempt)
            .where(
                PayoutAttempt.payout_id.in_(list(payout_ids)),
                PayoutAttempt.company_id == company_id,
            )
            .order_by(PayoutAttempt.payout_id, PayoutAttempt.attempt)
            .execution_options(populate_existing=True)
        )
        for a in (await session.scalars(stmt)).all():
            out[a.payout_id].append(a)
        return out


class AuditRepo:
    @staticmethod
    async def add(
        session: AsyncSession,
        company_id: UUID,
        *,
        actor_id: str,
        event: str,
        entity_type: str,
        entity_id: UUID,
        details: dict[str, Any],
    ) -> None:
        """One audit row; ``details`` holds IDs, counts and codes only (plan §6)."""
        ctx = trace.get_current_span().get_span_context()
        session.add(
            AuditEvent(
                id=new_id(),
                company_id=company_id,
                actor_id=actor_id,
                event=event,
                entity_type=entity_type,
                entity_id=entity_id,
                details=details,
                request_id=request_id_var.get(),
                trace_id=f"{ctx.trace_id:032x}" if ctx.is_valid else None,
            )
        )

    @staticmethod
    async def page(
        session: AsyncSession,
        company_id: UUID,
        *,
        entity_type: str | None,
        entity_id: UUID | None,
        event: str | None,
        after: UUID | None,
        limit: int,
    ) -> list[AuditEvent]:
        """Newest first (``id DESC``; UUIDv7 is time-ordered), ``limit + 1`` rows (§4.5, §4.9)."""
        stmt = select(AuditEvent).where(AuditEvent.company_id == company_id)
        if entity_type is not None:
            stmt = stmt.where(AuditEvent.entity_type == entity_type)
        if entity_id is not None:
            stmt = stmt.where(AuditEvent.entity_id == entity_id)
        if event is not None:
            stmt = stmt.where(AuditEvent.event == event)
        if after is not None:
            key = await _anchor(
                session,
                select(AuditEvent.id).where(
                    AuditEvent.id == after, AuditEvent.company_id == company_id
                ),
            )
            stmt = stmt.where(_after((AuditEvent.id,), key, descending=True))
        stmt = stmt.order_by(AuditEvent.id.desc()).limit(limit + 1)
        return list((await session.scalars(stmt)).all())

"""Employee use cases for S5: create, list and read (design §4.3; PAY-201, PAY-202, PAY-204).

PATCH and deactivation (PAY-203) arrive with F2.
"""

from __future__ import annotations

import logging
from typing import Final
from uuid import UUID

from pycommon.auth import Principal
from pycommon.db import new_id
from pycommon.logging import get_logger, log_event
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from payroll.api.pagination import decode_cursor, encode_cursor
from payroll.api.schemas import EmployeeCreate, EmployeeOut, Page
from payroll.crypto import employee_aad
from payroll.db.models import Employee
from payroll.db.repositories import AuditRepo, EmployeeRepo
from payroll.db.session import session_scope
from payroll.domain import EmployeeEmailTaken
from payroll.services.runtime import Runtime

__all__ = ["create_employee", "get_employee", "list_employees"]

_log: Final = get_logger("payroll.employees")
EMAIL_INDEX: Final = "uq_employees_company_email"


async def create_employee(rt: Runtime, principal: Principal, body: EmployeeCreate) -> EmployeeOut:
    """Encrypts both bank fields with the active kid; creating counts as a bank edit (R34)."""
    cid = principal.cid
    employee_id = new_id()
    emp = Employee(
        id=employee_id,
        company_id=cid,
        name=body.name,
        email=str(body.email),
        pay_type=body.pay_type,
        annual_salary_cents=body.annual_salary_cents,
        hourly_rate_cents=body.hourly_rate_cents,
        schedule=body.schedule,
        bank_routing_enc=rt.keyring.encrypt(
            body.bank_routing, aad=employee_aad("bank_routing", employee_id)
        ),
        bank_account_enc=rt.keyring.encrypt(
            body.bank_account, aad=employee_aad("bank_account", employee_id)
        ),
        account_last4=body.bank_account[-4:],
        bank_updated_by=principal.sub,
        bank_updated_at=func.now(),
        status="active",
        created_by=principal.sub,
    )
    async with session_scope(rt.sessions, "employees.create") as session:
        session.add(emp)
        try:
            await session.flush()
        except IntegrityError as exc:
            if EMAIL_INDEX in str(exc.orig):
                raise EmployeeEmailTaken() from None
            raise
        await session.refresh(emp)
        await AuditRepo.add(
            session,
            cid,
            actor_id=principal.sub,
            event="employee.created",
            entity_type="employee",
            entity_id=employee_id,
            details={
                "fields": sorted(body.model_fields_set),
                "account_last4": emp.account_last4,
            },
        )
        out = EmployeeOut.model_validate(emp)
    log_event(
        _log,
        logging.INFO,
        "employee.created",
        employee_id=str(employee_id),
        actor_id=principal.sub,
        pay_type=body.pay_type,
        schedule=body.schedule,
    )
    return out


async def list_employees(
    rt: Runtime, company_id: UUID, *, status: str | None, cursor: str | None, limit: int
) -> Page[EmployeeOut]:
    after = decode_cursor(cursor) if cursor is not None else None
    async with session_scope(rt.sessions, "employees.list") as session:
        rows = await EmployeeRepo.page(session, company_id, status=status, after=after, limit=limit)
        items = [EmployeeOut.model_validate(r) for r in rows[:limit]]
    next_cursor = encode_cursor(items[-1].id) if len(rows) > limit else None
    return Page[EmployeeOut](items=items, next_cursor=next_cursor)


async def get_employee(rt: Runtime, company_id: UUID, employee_id: UUID) -> EmployeeOut:
    async with session_scope(rt.sessions, "employees.get") as session:
        return EmployeeOut.model_validate(await EmployeeRepo.get(session, company_id, employee_id))

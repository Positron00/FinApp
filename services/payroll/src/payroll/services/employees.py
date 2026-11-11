"""Employee use cases: create, list, read and update (design §4.3; PAY-201 to PAY-204)."""

from __future__ import annotations

import logging
from typing import Any, Final
from uuid import UUID

from pycommon.auth import Principal
from pycommon.db import new_id
from pycommon.logging import get_logger, log_event
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from payroll.api.pagination import decode_cursor, encode_cursor
from payroll.api.schemas import EmployeeCreate, EmployeeOut, EmployeeUpdate, Page
from payroll.crypto import employee_aad
from payroll.db.models import Employee
from payroll.db.repositories import AuditRepo, EmployeeRepo
from payroll.db.session import session_scope
from payroll.domain import EmployeeEmailTaken
from payroll.domain.employees import PayBasis, check_bank_completeness, merge_pay_basis
from payroll.services.runtime import Runtime

__all__ = ["create_employee", "get_employee", "list_employees", "update_employee"]

_log: Final = get_logger("payroll.employees")
EMAIL_INDEX: Final = "uq_employees_company_email"
PLAIN_FIELDS: Final = ("name", "email", "schedule")
BANK_FIELDS: Final = ("bank_routing", "bank_account")


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


def _apply_fields(emp: Employee, body: EmployeeUpdate, changed: set[str]) -> None:
    """Plain fields and the merged pay basis; only values that differ from the row change."""
    supplied = body.model_fields_set
    for field in PLAIN_FIELDS:
        if field in supplied:
            new = str(getattr(body, field))
            if getattr(emp, field) != new:
                setattr(emp, field, new)
                changed.add(field)
    basis = merge_pay_basis(
        PayBasis(emp.pay_type, emp.annual_salary_cents, emp.hourly_rate_cents),
        supplied=supplied,
        pay_type=body.pay_type,
        annual_salary_cents=body.annual_salary_cents,
        hourly_rate_cents=body.hourly_rate_cents,
    )
    for field in ("pay_type", "annual_salary_cents", "hourly_rate_cents"):
        if getattr(emp, field) != getattr(basis, field):
            setattr(emp, field, getattr(basis, field))
            changed.add(field)


def _apply_bank(
    rt: Runtime, emp: Employee, body: EmployeeUpdate, actor: str, changed: set[str]
) -> dict[str, str | None] | None:
    """Re-encrypt each supplied bank field with the active kid and record the editor (PAY-203,
    PAY-204). Returns the ``account_last4`` audit diff, or ``None`` with no bank field."""
    supplied = [f for f in BANK_FIELDS if f in body.model_fields_set]
    if not supplied:
        return None
    old_last4 = emp.account_last4
    if body.bank_routing is not None:
        emp.bank_routing_enc = rt.keyring.encrypt(
            body.bank_routing, aad=employee_aad("bank_routing", emp.id)
        )
    if body.bank_account is not None:
        emp.bank_account_enc = rt.keyring.encrypt(
            body.bank_account, aad=employee_aad("bank_account", emp.id)
        )
        emp.account_last4 = body.bank_account[-4:]
    emp.bank_updated_by = actor
    emp.bank_updated_at = func.now()
    emp.bank_purged_at = None
    changed.update(supplied)
    return {"old": old_last4, "new": emp.account_last4}


async def update_employee(
    rt: Runtime, principal: Principal, employee_id: UUID, body: EmployeeUpdate
) -> EmployeeOut:
    """PATCH (PAY-203). One transaction: the employee row ``FOR UPDATE`` only (§4.10).

    - Plain fields and the merged pay basis are compared with the row; only real changes are
      written and audited (field names only).
    - A supplied bank field is always a bank change: it is re-encrypted with the active kid and
      sets ``bank_updated_by`` and ``bank_updated_at`` (PAY-203, ADR 0005). The stored value is
      never decrypted to compare (ADR 0004), so re-sending the same number still counts.
    - ``status: inactive`` sets ``deactivated_at``; ``status: active`` clears it (and
      ``bank_purged_at`` once new details arrive).
    - A PATCH that changes nothing writes no audit row.
    """
    cid = principal.cid
    async with session_scope(rt.sessions, "employees.update") as session:
        emp = await EmployeeRepo.lock(session, cid, employee_id)
        changed: set[str] = set()
        details: dict[str, Any] = {}
        _apply_fields(emp, body, changed)
        old_status = emp.status
        new_status = body.status or old_status
        check_bank_completeness(
            has_bank_details=emp.bank_account_enc is not None,
            supplied=body.model_fields_set,
            reactivating=old_status == "inactive" and new_status == "active",
        )
        last4 = _apply_bank(rt, emp, body, principal.sub, changed)
        if last4 is not None:
            details["account_last4"] = last4
        if new_status != old_status:
            emp.status = new_status
            emp.deactivated_at = func.now() if new_status == "inactive" else None
            changed.add("status")
            details["status"] = {"old": old_status, "new": new_status}
        if changed:
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
                event="employee.updated",
                entity_type="employee",
                entity_id=employee_id,
                details={"fields": sorted(changed), **details},
            )
        out = EmployeeOut.model_validate(emp)
    if changed:
        log_event(
            _log,
            logging.INFO,
            "employee.updated",
            employee_id=str(employee_id),
            actor_id=principal.sub,
            fields=sorted(changed),
            bank_changed=last4 is not None,
            status_changed="status" in changed,
        )
    return out

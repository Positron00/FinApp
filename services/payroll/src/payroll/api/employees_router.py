"""``/v1/employees`` for S5: create, list and read (design §4.3; PAY-201, PAY-202)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pycommon.auth import Principal

from payroll.api.deps import ANY_ROLE, PAYROLL_ADMIN, get_runtime, require_roles, require_web_user
from payroll.api.responses import errors
from payroll.api.schemas import EmployeeCreate, EmployeeOut, Limit, Page
from payroll.services import employees as svc
from payroll.services.runtime import Runtime

__all__ = ["router"]

router = APIRouter(
    prefix="/v1/employees", tags=["employees"], dependencies=[Depends(require_web_user)]
)
RT = Annotated[Runtime, Depends(get_runtime)]
AnyRole = Annotated[Principal, Depends(require_roles(*ANY_ROLE))]


@router.get("", response_model=Page[EmployeeOut], responses=errors())
async def list_employees(
    rt: RT,
    principal: AnyRole,
    status: Annotated[Literal["active", "inactive"] | None, Query()] = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[Limit, Query()] = 50,
) -> Page[EmployeeOut]:
    return await svc.list_employees(rt, principal.cid, status=status, cursor=cursor, limit=limit)


@router.post("", response_model=EmployeeOut, status_code=201, responses=errors(400, 409))
async def create_employee(
    body: EmployeeCreate,
    rt: RT,
    principal: Annotated[Principal, Depends(require_roles(PAYROLL_ADMIN))],
) -> EmployeeOut:
    return await svc.create_employee(rt, principal, body)


@router.get("/{employee_id}", response_model=EmployeeOut, responses=errors(404))
async def get_employee(employee_id: UUID, rt: RT, principal: AnyRole) -> EmployeeOut:
    return await svc.get_employee(rt, principal.cid, employee_id)

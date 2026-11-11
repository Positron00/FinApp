"""``GET`` / ``PUT /v1/company/settings`` (design §4.2; PAY-200)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pycommon.auth import Principal

from payroll.api.deps import ADMIN, ANY_ROLE, get_runtime, require_roles, require_web_user
from payroll.api.responses import errors
from payroll.api.schemas import CompanySettingsIn, CompanySettingsOut
from payroll.services import settings as svc
from payroll.services.runtime import Runtime

__all__ = ["router"]

router = APIRouter(
    prefix="/v1/company", tags=["settings"], dependencies=[Depends(require_web_user)]
)
RT = Annotated[Runtime, Depends(get_runtime)]


@router.get("/settings", response_model=CompanySettingsOut, responses=errors(404))
async def get_company_settings(
    rt: RT, principal: Annotated[Principal, Depends(require_roles(*ANY_ROLE))]
) -> CompanySettingsOut:
    return await svc.get_settings(rt, principal.cid)


@router.put("/settings", response_model=CompanySettingsOut, responses=errors(404, 409))
async def put_company_settings(
    body: CompanySettingsIn,
    rt: RT,
    principal: Annotated[Principal, Depends(require_roles(ADMIN))],
) -> CompanySettingsOut:
    return await svc.put_settings(rt, principal, body)

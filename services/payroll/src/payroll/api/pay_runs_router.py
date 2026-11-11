"""``/v1/pay-runs*`` (design §4.4): create, list, get, delete, hours, calculate, approve,
payslips, payouts, retry (PAY-500) and resolve (PAY-502).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from pycommon.auth import Principal

from payroll.api.deps import (
    ANY_ROLE,
    APPROVER,
    PAYROLL_ADMIN,
    IdempotencyHeader,
    get_runtime,
    idem_request,
    optional_idem_request,
    require_roles,
    require_web_user,
)
from payroll.api.responses import errors
from payroll.api.schemas import (
    HoursIn,
    HoursOut,
    Limit,
    Page,
    PayoutOut,
    PayoutStatusName,
    PayRunCreate,
    PayRunOut,
    PayslipOut,
    ResolveIn,
    RunStatusName,
)
from payroll.services import approve as approve_svc
from payroll.services import calculate as calculate_svc
from payroll.services import create_run as create_svc
from payroll.services import hours as hours_svc
from payroll.services import reads
from payroll.services import resolve as resolve_svc
from payroll.services import retry as retry_svc
from payroll.services.runtime import Runtime

__all__ = ["router"]

router = APIRouter(
    prefix="/v1/pay-runs", tags=["pay-runs"], dependencies=[Depends(require_web_user)]
)
RT = Annotated[Runtime, Depends(get_runtime)]
AnyRole = Annotated[Principal, Depends(require_roles(*ANY_ROLE))]
PayrollAdmin = Annotated[Principal, Depends(require_roles(PAYROLL_ADMIN))]
Approver = Annotated[Principal, Depends(require_roles(APPROVER))]
Cursor = Annotated[str | None, Query(max_length=512)]


@router.get("", response_model=Page[PayRunOut], responses=errors())
async def list_pay_runs(
    rt: RT,
    principal: AnyRole,
    status: Annotated[RunStatusName | None, Query()] = None,
    cursor: Cursor = None,
    limit: Annotated[Limit, Query()] = 50,
) -> Page[PayRunOut]:
    return await reads.list_runs(rt, principal.cid, status=status, cursor=cursor, limit=limit)


@router.post("", response_model=PayRunOut, status_code=201, responses=errors(400, 409))
async def create_pay_run(body: PayRunCreate, rt: RT, principal: PayrollAdmin) -> PayRunOut:
    return await create_svc.create_run(rt, principal, body)


@router.get("/{run_id}", response_model=PayRunOut, responses=errors(404))
async def get_pay_run(run_id: UUID, rt: RT, principal: AnyRole) -> PayRunOut:
    return await reads.get_run(rt, principal.cid, run_id)


@router.delete("/{run_id}", status_code=204, response_class=Response, responses=errors(404, 409))
async def delete_pay_run(run_id: UUID, rt: RT, principal: PayrollAdmin) -> Response:
    await create_svc.delete_run(rt, principal, run_id)
    return Response(status_code=204)


@router.put("/{run_id}/hours", response_model=HoursOut, responses=errors(400, 404, 409))
async def put_hours(run_id: UUID, body: HoursIn, rt: RT, principal: PayrollAdmin) -> HoursOut:
    return await hours_svc.put_hours(rt, principal, run_id, body)


@router.post("/{run_id}/calculate", response_model=PayRunOut, responses=errors(404, 409))
async def calculate_pay_run(run_id: UUID, rt: RT, principal: PayrollAdmin) -> PayRunOut:
    return await calculate_svc.calculate(rt, principal, run_id)


@router.post("/{run_id}/approve", response_model=PayRunOut, responses=errors(400, 404, 409))
async def approve_pay_run(
    *,
    run_id: UUID,
    request: Request,
    rt: RT,
    principal: Approver,
    idempotency_key: IdempotencyHeader = None,
) -> JSONResponse:
    idem = idem_request(request, principal, idempotency_key, rt)
    result = await approve_svc.approve(rt, principal, run_id, idem)
    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    return JSONResponse(result.body, status_code=result.status, headers=headers)


@router.get("/{run_id}/payslips", response_model=Page[PayslipOut], responses=errors(404))
async def list_payslips(
    run_id: UUID,
    rt: RT,
    principal: AnyRole,
    cursor: Cursor = None,
    limit: Annotated[Limit, Query()] = 50,
) -> Page[PayslipOut]:
    return await reads.list_payslips(rt, principal.cid, run_id, cursor=cursor, limit=limit)


@router.get("/{run_id}/payslips/{payslip_id}", response_model=PayslipOut, responses=errors(404))
async def get_payslip(run_id: UUID, payslip_id: UUID, rt: RT, principal: AnyRole) -> PayslipOut:
    return await reads.get_payslip(rt, principal.cid, run_id, payslip_id)


@router.get("/{run_id}/payouts", response_model=Page[PayoutOut], responses=errors(404))
async def list_payouts(
    *,
    run_id: UUID,
    rt: RT,
    principal: AnyRole,
    status: Annotated[PayoutStatusName | None, Query()] = None,
    cursor: Cursor = None,
    limit: Annotated[Limit, Query()] = 50,
) -> Page[PayoutOut]:
    return await reads.list_payouts(
        rt, principal.cid, run_id, status=status, cursor=cursor, limit=limit
    )


@router.post(
    "/{run_id}/payouts/{payout_id}/retry", response_model=PayoutOut, responses=errors(404, 409)
)
async def retry_payout(
    *,
    run_id: UUID,
    payout_id: UUID,
    request: Request,
    rt: RT,
    principal: Approver,
    idempotency_key: IdempotencyHeader = None,
) -> JSONResponse:
    """PAY-500. The ``Idempotency-Key`` is optional and honoured when present (§4.8, C52)."""
    idem = optional_idem_request(request, principal, idempotency_key, rt)
    result = await retry_svc.retry(rt, principal, run_id, payout_id, idem)
    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    return JSONResponse(result.body, status_code=result.status, headers=headers)


@router.post(
    "/{run_id}/payouts/{payout_id}/resolve",
    response_model=PayoutOut,
    responses=errors(400, 404, 409, retry_after=(409,)),
)
async def resolve_payout(
    run_id: UUID, payout_id: UUID, body: ResolveIn, rt: RT, principal: Approver
) -> PayoutOut:
    """PAY-502, R8: redrive or mark_rejected a needs_operator attempt (C48 wait for the latter)."""
    return await resolve_svc.resolve(rt, principal, run_id, payout_id, body)

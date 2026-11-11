"""Worker-only routes (design §4.6, §4.7; PAY-405, PAY-411): instruction and result."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pycommon.auth import Principal

from payroll.api.deps import IdempotencyHeader, get_runtime, idem_request, require_worker
from payroll.api.responses import errors
from payroll.api.schemas import InstructionOut, ResultIn, ResultOut
from payroll.services import instruction as instruction_svc
from payroll.services import record_result as result_svc
from payroll.services.runtime import Runtime

__all__ = ["router"]

router = APIRouter(
    prefix="/v1/internal/payouts", tags=["internal"], dependencies=[Depends(require_worker)]
)
RT = Annotated[Runtime, Depends(get_runtime)]
Worker = Annotated[Principal, Depends(require_worker)]


@router.get("/{payout_id}/instruction", response_model=InstructionOut, responses=errors(404, 409))
async def get_instruction(
    payout_id: UUID,
    rt: RT,
    principal: Worker,
    attempt: Annotated[int, Query(ge=1, le=32767)],
) -> JSONResponse:
    out = await instruction_svc.get_instruction(rt, principal.cid, payout_id, attempt)
    return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.post("/{payout_id}/result", response_model=ResultOut, responses=errors(400, 404, 409))
async def post_result(
    *,
    payout_id: UUID,
    body: ResultIn,
    request: Request,
    rt: RT,
    principal: Worker,
    idempotency_key: IdempotencyHeader = None,
) -> JSONResponse:
    idem = idem_request(request, principal, idempotency_key, rt, body=body.model_dump(mode="json"))
    result = await result_svc.record_result(rt, principal, payout_id, body, idem)
    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    return JSONResponse(result.body, status_code=result.status, headers=headers)

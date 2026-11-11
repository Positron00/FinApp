"""``POST /v1/invites/accept`` (identity LLD §3.4; PAY-103). Resend arrives with F1."""

from __future__ import annotations

from fastapi import APIRouter

from identity.api.deps import ClientIp, PublicCaller, Runtime
from identity.api.responses import errors
from identity.api.schemas import InviteAcceptIn, SessionUser
from identity.usecases.invites import AcceptCommand, accept_invite

__all__ = ["router"]

router = APIRouter(prefix="/v1/invites", tags=["invites"])


@router.post(
    "/accept",
    operation_id="invites_accept",
    response_model=SessionUser,
    responses=errors(400, 403, 429),
    summary="Accept an invite and set a password (caller: service:web)",
)
async def invites_accept(
    body: InviteAcceptIn, principal: PublicCaller, rt: Runtime, ip: ClientIp
) -> SessionUser:
    cmd = AcceptCommand(
        company_id=principal.cid, token=body.token, password=body.password, client_ip=ip
    )
    return await accept_invite(rt, cmd)

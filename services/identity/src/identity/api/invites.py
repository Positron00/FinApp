"""Invite routes (identity LLD §3.4; PAY-102, PAY-103). HTTP mapping only."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from identity.api.deps import AdminActor, ClientIp, PublicCaller, Runtime
from identity.api.responses import errors
from identity.api.schemas import InviteAcceptIn, PendingInviteOut, SessionUser
from identity.usecases.invites import AcceptCommand, ResendCommand, accept_invite, resend_invite

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


@router.post(
    "/{id}/resend",
    operation_id="invites_resend",
    response_model=PendingInviteOut,
    responses=errors(403, 404, 409, 429),
    summary="Revoke the user's pending invite and send a new one (admin)",
)
async def invites_resend(
    id: UUID, actor: AdminActor, rt: Runtime, ip: ClientIp
) -> PendingInviteOut:
    cmd = ResendCommand(
        company_id=actor.company_id, actor_user_id=actor.user_id, invite_id=id, client_ip=ip
    )
    return await resend_invite(rt, cmd)

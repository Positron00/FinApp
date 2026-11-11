"""``GET /v1/users/me`` and ``POST /v1/users/invite`` (identity LLD §3.4; PAY-101, PAY-107).

The admin list and PATCH arrive with F1.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from pycommon.errors import Forbidden

from identity.api.deps import AdminActor, Runtime, UserCaller
from identity.api.responses import errors
from identity.api.schemas import InviteUserIn, MeOut, UserOut
from identity.usecases.invites import InviteCommand, invite_user
from identity.usecases.users import get_me

__all__ = ["router"]

router = APIRouter(prefix="/v1/users", tags=["users"])


@router.get(
    "/me",
    operation_id="users_me",
    response_model=MeOut,
    responses=errors(403, 404, 429),
    summary="The calling user, whatever its status",
)
async def users_me(principal: UserCaller, rt: Runtime) -> MeOut:
    user_id = principal.user_id
    if user_id is None:  # pragma: no cover  (user_caller guarantees a UUID subject)
        raise Forbidden()
    return await get_me(rt, principal.cid, user_id)


@router.post(
    "/invite",
    operation_id="users_invite",
    status_code=status.HTTP_201_CREATED,
    response_model=UserOut,
    responses=errors(403, 409, 429),
    summary="Invite a user (admin)",
)
async def users_invite(body: InviteUserIn, actor: AdminActor, rt: Runtime) -> UserOut:
    cmd = InviteCommand(
        company_id=actor.company_id,
        actor_user_id=actor.user_id,
        email=body.email,
        name=body.name,
        roles=[str(r) for r in body.roles],
    )
    return await invite_user(rt, cmd)

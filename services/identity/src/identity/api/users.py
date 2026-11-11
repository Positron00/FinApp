"""User routes (identity LLD §3.4; PAY-101, PAY-107, PAY-108). HTTP mapping only."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, status
from pycommon.errors import Forbidden

from identity.api.deps import AdminActor, ClientIp, Runtime, UserCaller, route_label
from identity.api.pagination import Cursor, Limit
from identity.api.responses import errors
from identity.api.schemas import InviteUserIn, MeOut, PatchUserIn, UserListOut, UserOut
from identity.usecases.invites import InviteCommand, invite_user
from identity.usecases.users import UpdateCommand, get_me, list_users, update_user

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
    responses=errors(400, 403, 409, 429),
    summary="Invite a user (admin)",
)
async def users_invite(body: InviteUserIn, actor: AdminActor, rt: Runtime, ip: ClientIp) -> UserOut:
    cmd = InviteCommand(
        company_id=actor.company_id,
        actor_user_id=actor.user_id,
        email=body.email,
        name=body.name,
        roles=[str(r) for r in body.roles],
        client_ip=ip,
    )
    return await invite_user(rt, cmd)


@router.get(
    "",
    operation_id="users_list",
    response_model=UserListOut,
    responses=errors(403, 429),
    summary="Users in creation order (admin)",
)
async def users_list(
    actor: AdminActor, rt: Runtime, limit: Limit = 50, cursor: Cursor = None
) -> UserListOut:
    return await list_users(rt, actor.company_id, cursor, limit)


@router.patch(
    # uuid convertor: literal siblings (/invite, /me) must not match this route, so an
    # undeclared method on them gets 405, not 422 (Schemathesis ST204, F4a e2e run).
    "/{id:uuid}",
    operation_id="users_update",
    response_model=UserOut,
    responses=errors(400, 403, 404, 409, 429),
    summary="Change a user's roles or deactivate them (admin; not on yourself)",
)
async def users_update(
    id: UUID,
    body: PatchUserIn,
    *,
    request: Request,
    actor: AdminActor,
    rt: Runtime,
    ip: ClientIp,
) -> UserOut:
    cmd = UpdateCommand(
        company_id=actor.company_id,
        actor_user_id=actor.user_id,
        user_id=id,
        roles=None if body.roles is None else [str(r) for r in body.roles],
        deactivate=body.status == "inactive",
        route=route_label(request),
        client_ip=ip,
    )
    return await update_user(rt, cmd)

"""``POST /v1/auth/login`` (identity LLD §3.4; PAY-104). Reset routes arrive with F1."""

from __future__ import annotations

from fastapi import APIRouter

from identity.api.deps import ClientIp, PublicCaller, Runtime
from identity.api.responses import errors
from identity.api.schemas import LoginIn, SessionUser
from identity.usecases.signin import LoginCommand, login

__all__ = ["router"]

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post(
    "/login",
    operation_id="auth_login",
    response_model=SessionUser,
    responses=errors(400, 403, 429),
    summary="Sign in with email and password (caller: service:web)",
)
async def auth_login(
    body: LoginIn, principal: PublicCaller, rt: Runtime, ip: ClientIp
) -> SessionUser:
    cmd = LoginCommand(
        company_id=principal.cid, email=body.email, password=body.password, client_ip=ip
    )
    return await login(rt, cmd)

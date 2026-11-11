"""Sign-in and password reset routes (identity LLD §3.4; PAY-104, PAY-105). HTTP mapping only."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from identity.api.deps import ClientIp, PublicCaller, Runtime
from identity.api.responses import errors
from identity.api.schemas import (
    LoginIn,
    ResetConfirmIn,
    ResetConfirmOut,
    ResetRequestIn,
    SessionUser,
)
from identity.usecases.resets import ConfirmCommand, RequestCommand, confirm_reset, request_reset
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


@router.post(
    "/password-reset/request",
    operation_id="auth_password_reset_request",
    status_code=status.HTTP_202_ACCEPTED,
    response_class=Response,
    responses={**errors(400, 403, 429), 202: {"description": "Accepted, matched or not"}},
    summary="Request a password reset email (caller: service:web); always 202",
)
async def auth_password_reset_request(
    body: ResetRequestIn, principal: PublicCaller, rt: Runtime, ip: ClientIp
) -> Response:
    await request_reset(
        rt, RequestCommand(company_id=principal.cid, email=body.email, client_ip=ip)
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/password-reset/confirm",
    operation_id="auth_password_reset_confirm",
    response_model=ResetConfirmOut,
    responses=errors(400, 403, 429),
    summary="Set a new password with a reset token (caller: service:web)",
)
async def auth_password_reset_confirm(
    body: ResetConfirmIn, principal: PublicCaller, rt: Runtime, ip: ClientIp
) -> ResetConfirmOut:
    cmd = ConfirmCommand(
        company_id=principal.cid, token=body.token, password=body.password, client_ip=ip
    )
    return await confirm_reset(rt, cmd)

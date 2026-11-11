"""Sign-in (identity LLD §6.3-6.4; PAY-104, PAY-NFR-08, R15).

limiter → one user lookup → exactly one argon2 verify → audit, metrics, log. Every failure gives
the same 401 body, and unknown emails lock exactly like known ones.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pycommon.db import transaction
from pycommon.errors import RateLimited
from pycommon.logging import get_logger, log_event
from pycommon.otel import tracer

from identity.api.errors import InvalidCredentials
from identity.api.schemas import SessionUser
from identity.db.repositories import AuditRepo, CredentialRepo, UserRepo
from identity.domain import audit_events
from identity.limits import UNKNOWN_IP
from identity.runtime import IdentityRuntime
from identity.usecases.common import session_user

__all__ = ["LoginCommand", "login"]

_log = get_logger("identity.signin")


@dataclass(frozen=True)
class LoginCommand:
    company_id: UUID
    email: str = field(repr=False)
    password: str = field(repr=False)
    client_ip: str | None = field(repr=False)


async def login(rt: IdentityRuntime, cmd: LoginCommand) -> SessionUser:
    with tracer("identity").start_as_current_span("identity.login") as span:
        try:
            user = await _login(rt, cmd)
        except RateLimited:
            span.set_attribute("identity.outcome", "rate_limited")
            raise
        except InvalidCredentials:
            span.set_attribute("identity.outcome", "failure")
            raise
        span.set_attribute("identity.outcome", "success")
        span.set_attribute("identity.user_id", str(user.user_id))
        return user


async def _login(rt: IdentityRuntime, cmd: LoginCommand) -> SessionUser:
    cid = cmd.company_id
    ih = rt.ip_hash(cmd.client_ip)
    ek = rt.email_key(cmd.email)
    with tracer("identity").start_as_current_span(
        "identity.ratelimit.check", attributes={"identity.component": "login"}
    ) as span:
        gate = await rt.limits.check_login(ih or UNKNOWN_IP, ek)
        span.set_attribute("identity.allowed", gate.allowed)
    if not gate.allowed:
        if gate.lock_started is not None:
            await _audit_lockout(rt, cmd, ih, gate.lock_started.level, gate.retry_after_s)
        rt.metrics.signins.add(1, {"outcome": "rate_limited"})
        log_event(
            _log,
            logging.WARNING,
            "auth.signin.rate_limited",
            scope=gate.scope,
            ip_hash=ih,
            retry_after_s=gate.retry_after_s,
        )
        raise RateLimited(retry_after_s=gate.retry_after_s)

    async with rt.sessions() as session:
        row = await UserRepo(session).find_for_login(cid, cmd.email)
    usable = row is not None and row.user.status == "active" and row.password_hash is not None
    result = await rt.hasher.verify(row.password_hash if usable and row else None, cmd.password)

    if row is None:
        reason = "unknown_user"
    elif not usable:
        reason = "not_active"
    elif not result.ok:
        reason = "bad_password"
    else:
        reason = None

    user_id = row.user.id if row is not None else None
    if reason is not None:
        # Only failed verifies count toward the per-account limit (C63).
        await rt.limits.record_failure(gate)
        async with transaction(rt.sessions) as session:
            await AuditRepo(session).append(
                cid=cid,
                event=audit_events.AUTH_SIGNIN_FAILED,
                target_user_id=user_id,
                ip_hash=ih,
                details={"reason": reason},
            )
        rt.metrics.signins.add(1, {"outcome": "failure"})
        fields: dict[str, Any] = {"reason": reason, "ip_hash": ih}
        if user_id is not None:
            fields["user_id"] = str(user_id)
        log_event(_log, logging.INFO, "auth.signin.failed", **fields)
        raise InvalidCredentials()

    if row is None:  # pragma: no cover  (reason is "unknown_user" when row is None)
        raise InvalidCredentials()
    user = row.user
    rehashed = False
    new_hash = await rt.hasher.hash(cmd.password) if result.needs_rehash else None
    async with transaction(rt.sessions) as session:
        if new_hash is not None:
            await CredentialRepo(session).rehash(user_id=user.id, cid=cid, password_hash=new_hash)
            rehashed = True
        await AuditRepo(session).append(
            cid=cid,
            event=audit_events.AUTH_SIGNIN_SUCCEEDED,
            actor_user_id=user.id,
            target_user_id=user.id,
            ip_hash=ih,
        )
    await rt.limits.clear_level(ek)
    rt.metrics.signins.add(1, {"outcome": "success"})
    log_event(
        _log,
        logging.INFO,
        "auth.signin.succeeded",
        user_id=str(user.id),
        ip_hash=ih,
        rehashed=rehashed,
    )
    return session_user(user)


async def _audit_lockout(
    rt: IdentityRuntime, cmd: LoginCommand, ih: str | None, level: int, lock_s: int
) -> None:
    async with transaction(rt.sessions) as session:
        target = await UserRepo(session).find_by_email(cmd.company_id, cmd.email)
        target_id = target.id if target is not None else None
        await AuditRepo(session).append(
            cid=cmd.company_id,
            event=audit_events.AUTH_LOCKOUT,
            target_user_id=target_id,
            ip_hash=ih,
            details={"lock_seconds": lock_s, "level": level},
        )
    # "level" is a base field of every log line (and log_event's own parameter): use lock_level.
    fields: dict[str, Any] = {"lock_level": level, "lock_seconds": lock_s, "ip_hash": ih}
    if target_id is not None:
        fields["user_id"] = str(target_id)
    log_event(_log, logging.WARNING, "auth.lockout", **fields)

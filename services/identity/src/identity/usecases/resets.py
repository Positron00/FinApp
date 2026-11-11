"""Password reset request and confirm (identity LLD §6.6; PAY-105, PAY-107, R17, R19, C31).

The request always answers 202 after one user lookup, whether or not the email matched, and
pads its database work to ``REQUEST_FLOOR_S`` so a match isn't slower than a miss (C74). The
relay issues the token at send time, so the request stores no token. Confirm checks the token
before the password policy, then marks the reset used, replaces the credential and bumps
``roles_version`` in one transaction, so the BFF's re-check ends any session it missed; it then
clears the account's login lockout (C74).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID

from pycommon.db import transaction
from pycommon.errors import RateLimited
from pycommon.logging import get_logger, log_event
from pycommon.otel import inject_traceparent, tracer

from identity.api.errors import PasswordTooWeak, ResetInvalid
from identity.api.schemas import ResetConfirmOut
from identity.db.repositories import AuditRepo, CredentialRepo, OutboxRepo, ResetRepo, UserRepo
from identity.domain import audit_events
from identity.domain.passwords import PasswordTooWeakError, check_password_policy
from identity.domain.tokens import digest_of
from identity.limits import UNKNOWN_IP
from identity.runtime import IdentityRuntime

__all__ = ["REQUEST_FLOOR_S", "ConfirmCommand", "RequestCommand", "confirm_reset", "request_reset"]

_log = get_logger("identity.resets")

# The least time a reset request's lookup and writes take (C74). An active match runs a write
# transaction (about 3 ms more than a miss on the dev box); padding every request past the
# lookup to this floor puts both in one timing class, well clear of the match's own cost.
REQUEST_FLOOR_S: Final = 0.05


@dataclass(frozen=True)
class RequestCommand:
    company_id: UUID
    email: str = field(repr=False)
    client_ip: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ConfirmCommand:
    company_id: UUID
    token: str = field(repr=False)
    password: str = field(repr=False)
    client_ip: str | None = field(default=None, repr=False)


async def request_reset(rt: IdentityRuntime, cmd: RequestCommand) -> None:
    """``reset_ip`` and ``reset_account`` limits, then a reset and email for an active user."""
    cid = cmd.company_id
    ih = rt.ip_hash(cmd.client_ip)
    retry = await rt.limits.check_reset(ih or UNKNOWN_IP, rt.email_key(cmd.email))
    if retry is not None:
        raise RateLimited(retry_after_s=retry)
    loop = asyncio.get_running_loop()
    with tracer("identity").start_as_current_span("identity.reset.request") as span:
        started = loop.time()
        async with rt.sessions() as session:
            found = await UserRepo(session).find_by_email(cid, cmd.email)
        reset_id: UUID | None = None
        email_id: UUID | None = None
        if found is not None and found.status == "active":
            async with transaction(rt.sessions) as session:
                # Lock the user so a concurrent deactivation or request is serialised (§6.5).
                user = await UserRepo(session).lock(cid, found.id)
                if user is not None and user.status == "active":
                    resets, outbox = ResetRepo(session), OutboxRepo(session)
                    revoked = await resets.revoke_pending_for_user(cid, user.id)
                    if revoked is not None:
                        await outbox.cancel_pending(password_reset_id=revoked)
                    reset_id = await resets.insert(cid=cid, user_id=user.id)
                    email_id = await outbox.queue(
                        cid=cid,
                        template="password_reset",
                        recipient=user.email,
                        password_reset_id=reset_id,
                        traceparent=inject_traceparent(),
                    )
                    await AuditRepo(session).append(
                        cid=cid,
                        event=audit_events.AUTH_PASSWORD_RESET_REQUESTED,
                        target_user_id=user.id,
                        ip_hash=ih,
                        details={"reset_id": str(reset_id)},
                    )
        if reset_id is not None and found is not None:
            span.set_attribute("identity.user_id", str(found.id))
        # Same latency for a match and a miss (C74): sleep out the rest of the floor.
        await asyncio.sleep(max(0.0, REQUEST_FLOOR_S - (loop.time() - started)))
    fields: dict[str, Any] = {"matched": reset_id is not None, "ip_hash": ih}
    if reset_id is not None and found is not None:
        fields["user_id"] = str(found.id)
        fields["reset_id"] = str(reset_id)
    log_event(_log, logging.INFO, "auth.password_reset.requested", **fields)
    if email_id is not None and found is not None:
        log_event(
            _log,
            logging.INFO,
            "email.queued",
            email_id=str(email_id),
            template="password_reset",
            user_id=str(found.id),
        )


def _invalid(reason: str) -> ResetInvalid:
    log_event(_log, logging.INFO, "auth.password_reset.invalid", reason=reason)
    return ResetInvalid()


async def confirm_reset(rt: IdentityRuntime, cmd: ConfirmCommand) -> ResetConfirmOut:
    """``accept_ip`` limit (R18); token before policy; one conditional transaction (§6.6)."""
    cid = cmd.company_id
    ih = rt.ip_hash(cmd.client_ip)
    retry = await rt.limits.check_accept(ih or UNKNOWN_IP)
    if retry is not None:
        raise RateLimited(retry_after_s=retry)
    with tracer("identity").start_as_current_span("identity.reset.confirm") as span:
        digest = digest_of(cmd.token)
        if digest is None:
            raise _invalid("malformed")
        async with rt.sessions() as session:
            found = await ResetRepo(session).find_for_confirm(cid, digest)
        if found is None:
            raise _invalid("unknown")
        if found.reset_status != "pending":
            raise _invalid("not_pending")
        if not found.unexpired:
            raise _invalid("expired")
        if found.user_status != "active":
            raise _invalid("user_not_active")
        span.set_attribute("identity.user_id", str(found.user_id))
        try:
            check_password_policy(cmd.password)
        except PasswordTooWeakError:
            raise PasswordTooWeak() from None
        password_hash = await rt.hasher.hash(cmd.password)
        async with transaction(rt.sessions) as session:
            users = UserRepo(session)
            # Users before resets (§6.5): a concurrent deactivation is serialised here.
            user = await users.lock(cid, found.user_id)
            if await ResetRepo(session).mark_used(cid, found.reset_id) is None:
                # A concurrent confirm won, a deactivation revoked it, or it expired meanwhile.
                raise _invalid("not_pending")
            roles_version = await users.bump_roles_version_if_active(cid, found.user_id)
            if user is None or roles_version is None:  # pragma: no cover  (deactivation revokes)
                raise _invalid("user_not_active")  # rolls the transaction back
            await CredentialRepo(session).set_password(
                user_id=found.user_id, cid=cid, password_hash=password_hash
            )
            await AuditRepo(session).append(
                cid=cid,
                event=audit_events.AUTH_PASSWORD_RESET_COMPLETED,
                actor_user_id=found.user_id,
                target_user_id=found.user_id,
                ip_hash=ih,
                details={"reset_id": str(found.reset_id)},
            )
    # After the commit, so a rolled-back confirm never unlocks the account (C74, §6.4).
    await rt.limits.clear_lockout(rt.email_key(user.email))
    log_event(
        _log,
        logging.INFO,
        "auth.password_reset.completed",
        user_id=str(found.user_id),
        reset_id=str(found.reset_id),
        roles_version=roles_version,
    )
    return ResetConfirmOut(user_id=found.user_id, roles_version=roles_version)

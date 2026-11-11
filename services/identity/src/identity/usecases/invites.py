"""Invite, resend and accept (identity LLD §6.6; PAY-101, PAY-102, PAY-103, R17, C31).

Lock order everywhere is users before invites and resets (§6.5), so resend, accept and a
concurrent deactivation can't deadlock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

import sqlalchemy.exc
from pycommon.db import transaction
from pycommon.errors import RateLimited
from pycommon.logging import get_logger, log_event
from pycommon.otel import inject_traceparent, tracer

from identity.api.errors import (
    InviteInvalid,
    InviteNotFound,
    InviteNotPending,
    PasswordTooWeak,
    UserEmailTaken,
)
from identity.api.schemas import PendingInviteOut, SessionUser, UserOut
from identity.db.repositories import AuditRepo, CredentialRepo, InviteRepo, OutboxRepo, UserRepo
from identity.domain import audit_events
from identity.domain.passwords import PasswordTooWeakError, check_password_policy
from identity.domain.roles import normalise_roles
from identity.domain.tokens import digest_of
from identity.limits import UNKNOWN_IP
from identity.runtime import IdentityRuntime
from identity.usecases.common import session_user, user_out

__all__ = [
    "AcceptCommand",
    "InviteCommand",
    "ResendCommand",
    "accept_invite",
    "invite_user",
    "resend_invite",
]

_log = get_logger("identity.invites")


@dataclass(frozen=True)
class InviteCommand:
    company_id: UUID
    actor_user_id: UUID
    email: str = field(repr=False)
    name: str = field(repr=False)
    roles: list[str]
    client_ip: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class AcceptCommand:
    company_id: UUID
    token: str = field(repr=False)
    password: str = field(repr=False)
    client_ip: str | None = field(repr=False)


@dataclass(frozen=True)
class ResendCommand:
    company_id: UUID
    actor_user_id: UUID
    invite_id: UUID
    client_ip: str | None = field(default=None, repr=False)


def _is_email_taken(exc: sqlalchemy.exc.IntegrityError) -> bool:
    return "uq_users_company_email" in str(exc.orig)


async def invite_user(rt: IdentityRuntime, cmd: InviteCommand) -> UserOut:
    """One transaction: pending user, invite (no token yet), outbox row, audit (§6.6)."""
    cid = cmd.company_id
    roles = normalise_roles(cmd.roles)
    ih = rt.ip_hash(cmd.client_ip)
    with tracer("identity").start_as_current_span("identity.invite.create") as span:
        try:
            async with transaction(rt.sessions) as session:
                user = await UserRepo(session).insert(
                    cid=cid,
                    email=cmd.email,
                    name=cmd.name,
                    roles=roles,
                    status="pending",
                    created_by=cmd.actor_user_id,
                )
                invite_id = await InviteRepo(session).insert(
                    cid=cid, user_id=user.id, created_by=cmd.actor_user_id
                )
                email_id = await OutboxRepo(session).queue(
                    cid=cid,
                    template="invite",
                    recipient=user.email,
                    invite_id=invite_id,
                    traceparent=inject_traceparent(),
                )
                await AuditRepo(session).append(
                    cid=cid,
                    event=audit_events.USER_INVITED,
                    actor_user_id=cmd.actor_user_id,
                    target_user_id=user.id,
                    ip_hash=ih,
                    details={"roles": roles, "invite_id": str(invite_id)},
                )
        except sqlalchemy.exc.IntegrityError as exc:
            if _is_email_taken(exc):
                raise UserEmailTaken() from None
            raise
        span.set_attribute("identity.user_id", str(user.id))
        span.set_attribute("identity.invite_id", str(invite_id))
    log_event(
        _log,
        logging.INFO,
        "user.invited",
        user_id=str(user.id),
        invite_id=str(invite_id),
        actor_user_id=str(cmd.actor_user_id),
        roles=roles,
    )
    log_event(
        _log,
        logging.INFO,
        "email.queued",
        email_id=str(email_id),
        template="invite",
        user_id=str(user.id),
    )
    return user_out(user, invite_id)


async def resend_invite(rt: IdentityRuntime, cmd: ResendCommand) -> PendingInviteOut:
    """One transaction (§6.6): revoke the user's pending invite and its pending emails, then a
    new invite (no token until the relay sends it) and outbox row; audit ``user.invite_resent``.

    ``invite_id`` may name any invite of the user (a revoked or expired one too), so a stale UI
    id still works.
    """
    cid = cmd.company_id
    ih = rt.ip_hash(cmd.client_ip)
    with tracer("identity").start_as_current_span("identity.invite.resend") as span:
        async with transaction(rt.sessions) as session:
            invites = InviteRepo(session)
            old = await invites.get(cid, cmd.invite_id)
            if old is None:
                raise InviteNotFound()
            user = await UserRepo(session).lock(cid, old.user_id)
            if user is None or user.status != "pending":
                raise InviteNotPending()
            outbox = OutboxRepo(session)
            revoked_id = await invites.revoke_pending_for_user(cid, user.id)
            if revoked_id is not None:
                await outbox.cancel_pending(invite_id=revoked_id)
            fresh_id = await invites.insert(cid=cid, user_id=user.id, created_by=cmd.actor_user_id)
            email_id = await outbox.queue(
                cid=cid,
                template="invite",
                recipient=user.email,
                invite_id=fresh_id,
                traceparent=inject_traceparent(),
            )
            await AuditRepo(session).append(
                cid=cid,
                event=audit_events.USER_INVITE_RESENT,
                actor_user_id=cmd.actor_user_id,
                target_user_id=user.id,
                ip_hash=ih,
                details={
                    "invite_id": str(fresh_id),
                    "revoked_invite_id": None if revoked_id is None else str(revoked_id),
                },
            )
        span.set_attribute("identity.user_id", str(user.id))
        span.set_attribute("identity.invite_id", str(fresh_id))
    fields: dict[str, str] = {
        "user_id": str(user.id),
        "invite_id": str(fresh_id),
        "actor_user_id": str(cmd.actor_user_id),
    }
    if revoked_id is not None:
        fields["revoked_invite_id"] = str(revoked_id)
    log_event(_log, logging.INFO, "user.invite_resent", **fields)
    log_event(
        _log,
        logging.INFO,
        "email.queued",
        email_id=str(email_id),
        template="invite",
        user_id=str(user.id),
    )
    return PendingInviteOut(id=fresh_id, expires_at=None, email_status="pending")


def _invalid(reason: str) -> InviteInvalid:
    log_event(_log, logging.INFO, "invite.invalid", reason=reason)
    return InviteInvalid()


async def accept_invite(rt: IdentityRuntime, cmd: AcceptCommand) -> SessionUser:
    """Token before password policy; the state change is one conditional transaction (§6.6)."""
    cid = cmd.company_id
    ih = rt.ip_hash(cmd.client_ip)
    retry = await rt.limits.check_accept(ih or UNKNOWN_IP)
    if retry is not None:
        raise RateLimited(retry_after_s=retry)
    with tracer("identity").start_as_current_span("identity.invite.accept") as span:
        digest = digest_of(cmd.token)
        if digest is None:
            raise _invalid("malformed")
        async with rt.sessions() as session:
            found = await InviteRepo(session).find_for_accept(cid, digest)
        if found is None:
            raise _invalid("unknown")
        if found.invite_status != "pending":
            raise _invalid("not_pending")
        if not found.unexpired:
            raise _invalid("expired")
        if found.user_status != "pending":
            raise _invalid("user_not_pending")
        span.set_attribute("identity.invite_id", str(found.invite_id))
        span.set_attribute("identity.user_id", str(found.user_id))
        try:
            check_password_policy(cmd.password)
        except PasswordTooWeakError:
            raise PasswordTooWeak() from None
        password_hash = await rt.hasher.hash(cmd.password)
        async with transaction(rt.sessions) as session:
            # Users before invites (§6.5): a concurrent resend or deactivation waits here.
            await UserRepo(session).lock(cid, found.user_id)
            user_id = await InviteRepo(session).mark_accepted(cid, found.invite_id)
            user = await UserRepo(session).activate_pending(cid, user_id) if user_id else None
            if user is None:
                # A concurrent accept won, or the invite expired meanwhile: roll back.
                raise _invalid("not_pending")
            await CredentialRepo(session).insert(
                user_id=user.id, cid=cid, password_hash=password_hash
            )
            await AuditRepo(session).append(
                cid=cid,
                event=audit_events.USER_INVITE_ACCEPTED,
                actor_user_id=user.id,
                target_user_id=user.id,
                ip_hash=ih,
                details={"invite_id": str(found.invite_id)},
            )
    # Failed logins against the pending account may have locked its email; only the invitee
    # holds the token, so accepting clears it (C74, §6.4). After the commit, best effort.
    await rt.limits.clear_lockout(rt.email_key(user.email))
    log_event(
        _log,
        logging.INFO,
        "user.invite_accepted",
        user_id=str(user.id),
        invite_id=str(found.invite_id),
    )
    return session_user(user)

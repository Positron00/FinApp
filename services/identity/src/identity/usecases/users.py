"""User reads and admin (identity LLD §3.4, §6.5; PAY-107, PAY-108, PAY-601)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pycommon.db import transaction
from pycommon.errors import Forbidden
from pycommon.logging import get_logger, log_event
from pycommon.otel import tracer
from sqlalchemy.ext.asyncio import AsyncSession

from identity.api.errors import CannotModifySelf, LastAdmin, UserNotFound
from identity.api.pagination import decode_cursor, encode_cursor
from identity.api.schemas import MeOut, UserListOut, UserOut
from identity.db.repositories import AuditRepo, InviteRepo, OutboxRepo, ResetRepo, UserRepo
from identity.domain import audit_events
from identity.domain.users import (
    PatchRequest,
    TargetUser,
    UpdateRefused,
    UserChange,
    plan_user_update,
)
from identity.runtime import IdentityRuntime
from identity.usecases.common import me_out, user_out

__all__ = ["UpdateCommand", "get_me", "list_users", "update_user"]

_log = get_logger("identity.users")


@dataclass(frozen=True)
class UpdateCommand:
    company_id: UUID
    actor_user_id: UUID
    user_id: UUID
    roles: list[str] | None
    deactivate: bool
    route: str  # the matched route template, for auth.forbidden (same label as admin_actor)
    client_ip: str | None = field(default=None, repr=False)


async def get_me(rt: IdentityRuntime, cid: UUID, user_id: UUID) -> MeOut:
    """The caller's row, whatever its status (the BFF ends sessions that aren't active)."""
    async with rt.sessions() as session:
        user = await UserRepo(session).get(cid, user_id)
        if user is None:
            raise UserNotFound()
        pending = await InviteRepo(session).pending_id_for(cid, user_id)
    return me_out(user, pending)


async def list_users(rt: IdentityRuntime, cid: UUID, cursor: str | None, limit: int) -> UserListOut:
    """Creation order; ``pending_invite_id`` for pending users (R21, C11)."""
    after = decode_cursor(cursor)
    async with rt.sessions() as session:
        rows = await UserRepo(session).list_page(cid, after=after, limit=limit)
        page = rows[:limit]
        pending_ids = [u.id for u in page if u.status == "pending"]
        invites = await InviteRepo(session).pending_ids_for(cid, pending_ids)
    items = [user_out(u, invites.get(u.id)) for u in page]
    next_cursor = encode_cursor(page[-1].id) if len(rows) > limit else None
    return UserListOut(items=items, next_cursor=next_cursor)


def _refused(cmd: UpdateCommand, code: str) -> None:
    log_event(
        _log,
        logging.INFO,
        "user.update_refused",
        user_id=str(cmd.user_id),
        actor_user_id=str(cmd.actor_user_id),
        code=code,
    )


async def update_user(rt: IdentityRuntime, cmd: UpdateCommand) -> UserOut:
    """``PATCH /v1/users/{id}`` in one transaction (§6.5).

    The self rule is checked before any lookup. Then the active admins are locked in ``id``
    order, then the target, so two admins demoting each other serialise: the second finds its
    actor no longer an admin (403), and the company never loses its last admin.
    """
    cid = cmd.company_id
    if cmd.user_id == cmd.actor_user_id:
        _refused(cmd, CannotModifySelf.code)
        raise CannotModifySelf()
    ih = rt.ip_hash(cmd.client_ip)
    with tracer("identity").start_as_current_span(
        "identity.user.update", attributes={"identity.user_id": str(cmd.user_id)}
    ) as span:
        revoked_invite: UUID | None = None
        async with transaction(rt.sessions) as session:
            users = UserRepo(session)
            admins = await users.lock_active_admin_ids(cid)
            target = await users.lock(cid, cmd.user_id)
            if target is None:
                raise UserNotFound()
            if cmd.actor_user_id not in admins:
                # Demoted or deactivated since admin_actor read the DB (a concurrent PATCH).
                log_event(
                    _log,
                    logging.INFO,
                    "auth.forbidden",
                    route=cmd.route,
                    reason="db_role",
                )
                raise Forbidden()
            others = sum(1 for a in admins if a != target.id)
            try:
                change = plan_user_update(
                    cmd.actor_user_id,
                    TargetUser(id=target.id, roles=target.roles, status=target.status),
                    PatchRequest(roles=cmd.roles, deactivate=cmd.deactivate),
                    others,
                )
            except UpdateRefused as refused:  # pragma: no cover  (defence in depth, below)
                # Unreachable through the API: the actor is itself an active admin other than
                # the target (self rule, then the re-check above), so one always remains. Kept
                # so the rule holds if those checks ever change; unit/test_user_rules.py.
                _refused(cmd, refused.code)
                if refused.code == LastAdmin.code:
                    raise LastAdmin() from None
                raise CannotModifySelf() from None
            user = target
            if not change.noop:
                user = await users.apply_change(
                    cid, target.id, roles=change.new_roles, deactivate=change.deactivate
                )
                if change.deactivate:
                    revoked_invite = await _revoke_pending(session, cid, target.id)
                await _audit(session, cmd, change, ih)
            pending = await InviteRepo(session).pending_id_for(cid, user.id)
        span.set_attribute("identity.roles_changed", change.new_roles is not None)
        span.set_attribute("identity.deactivated", change.deactivate)
    _log_change(cmd, change, user.roles_version, revoked_invite)
    return user_out(user, pending)


async def _revoke_pending(session: AsyncSession, cid: UUID, user_id: UUID) -> UUID | None:
    """Deactivation revokes the user's pending invite and reset and cancels their emails."""
    outbox = OutboxRepo(session)
    invite_id = await InviteRepo(session).revoke_pending_for_user(cid, user_id)
    if invite_id is not None:
        await outbox.cancel_pending(invite_id=invite_id)
    reset_id = await ResetRepo(session).revoke_pending_for_user(cid, user_id)
    if reset_id is not None:
        await outbox.cancel_pending(password_reset_id=reset_id)
    return invite_id


async def _audit(
    session: AsyncSession, cmd: UpdateCommand, change: UserChange, ih: str | None
) -> None:
    audit = AuditRepo(session)
    if change.new_roles is not None:
        await audit.append(
            cid=cmd.company_id,
            event=audit_events.USER_ROLES_CHANGED,
            actor_user_id=cmd.actor_user_id,
            target_user_id=cmd.user_id,
            ip_hash=ih,
            details={"old_roles": change.old_roles, "new_roles": change.new_roles},
        )
    if change.deactivate:
        await audit.append(
            cid=cmd.company_id,
            event=audit_events.USER_DEACTIVATED,
            actor_user_id=cmd.actor_user_id,
            target_user_id=cmd.user_id,
            ip_hash=ih,
        )


def _log_change(
    cmd: UpdateCommand, change: UserChange, roles_version: int, revoked_invite: UUID | None
) -> None:
    base = {"user_id": str(cmd.user_id), "actor_user_id": str(cmd.actor_user_id)}
    if change.new_roles is not None:
        log_event(
            _log,
            logging.INFO,
            "user.roles_changed",
            **base,
            old_roles=change.old_roles,
            new_roles=change.new_roles,
            roles_version=roles_version,
        )
    if change.deactivate:
        fields: dict[str, Any] = {**base, "roles_version": roles_version}
        if revoked_invite is not None:
            fields["revoked_invite_id"] = str(revoked_invite)
        log_event(_log, logging.INFO, "user.deactivated", **fields)

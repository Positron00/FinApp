"""Repositories (identity LLD §1). Every user-facing query takes ``company_id`` explicitly."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pycommon.db import new_id
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from identity.db.models import AuditLog, Credential, EmailOutbox, Invite, PasswordReset, User

__all__ = [
    "AuditRepo",
    "ClaimedEmail",
    "CredentialRepo",
    "InviteForAccept",
    "InviteRepo",
    "IssuedFor",
    "LoginRow",
    "OutboxRepo",
    "ResetForConfirm",
    "ResetRepo",
    "UserRepo",
]


@dataclass(frozen=True)
class LoginRow:
    user: User
    password_hash: str | None


class UserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def find_for_login(self, cid: UUID, email: str) -> LoginRow | None:
        """One query: the user by ``(company_id, lower(email))`` joined to its credential."""
        stmt = (
            select(User, Credential.password_hash)
            .outerjoin(Credential, Credential.user_id == User.id)
            .where(User.company_id == cid, func.lower(User.email) == email.strip().lower())
        )
        row = (await self._s.execute(stmt)).first()
        if row is None:
            return None
        return LoginRow(user=row[0], password_hash=row[1])

    async def find_by_email(self, cid: UUID, email: str) -> User | None:
        stmt = select(User).where(
            User.company_id == cid, func.lower(User.email) == email.strip().lower()
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get(self, cid: UUID, user_id: UUID) -> User | None:
        stmt = select(User).where(User.company_id == cid, User.id == user_id)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def any_exists(self) -> bool:
        """Any user in any company (the bootstrap guard, §3.8)."""
        return (
            await self._s.execute(text("SELECT 1 FROM identity.users LIMIT 1"))
        ).first() is not None

    async def insert(
        self,
        *,
        cid: UUID,
        email: str,
        name: str,
        roles: Sequence[str],
        status: str,
        created_by: UUID | None,
        activated: bool = False,
    ) -> User:
        values: dict[str, Any] = {
            "id": new_id(),
            "company_id": cid,
            "email": email.strip(),
            "name": name,
            "roles": list(roles),
            "status": status,
            "created_by": created_by,
        }
        if activated:
            values["activated_at"] = func.now()
        stmt = insert(User).values(**values).returning(User)
        return (await self._s.execute(stmt)).scalar_one()

    async def lock(self, cid: UUID, user_id: UUID) -> User | None:
        """The row, locked ``FOR UPDATE`` (lock order: users before invites and resets, §6.5)."""
        stmt = select(User).where(User.company_id == cid, User.id == user_id).with_for_update()
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def lock_active_admin_ids(self, cid: UUID) -> list[UUID]:
        """Every active admin of the company, locked in ``id`` order (§6.5 step 3)."""
        stmt = (
            select(User.id)
            .where(
                User.company_id == cid,
                User.status == "active",
                User.roles.contains(["admin"]),
            )
            .order_by(User.id)
            .with_for_update()
        )
        return list((await self._s.execute(stmt)).scalars())

    async def list_page(self, cid: UUID, *, after: UUID | None, limit: int) -> list[User]:
        """Creation order (``id`` ascending); ``limit + 1`` rows tell whether a page follows."""
        stmt = select(User).where(User.company_id == cid)
        if after is not None:
            stmt = stmt.where(User.id > after)
        stmt = stmt.order_by(User.id).limit(limit + 1)
        return list((await self._s.execute(stmt)).scalars())

    async def apply_change(
        self, cid: UUID, user_id: UUID, *, roles: Sequence[str] | None, deactivate: bool
    ) -> User:
        """Write a planned PATCH and bump ``roles_version`` by exactly 1 (§6.5)."""
        values: dict[str, Any] = {
            "roles_version": User.roles_version + 1,
            "updated_at": func.now(),
        }
        if roles is not None:
            values["roles"] = list(roles)
        if deactivate:
            values["status"] = "inactive"
            values["deactivated_at"] = func.now()
        stmt = (
            update(User)
            .where(User.company_id == cid, User.id == user_id)
            .values(**values)
            .returning(User)
        )
        return (await self._s.execute(stmt)).scalar_one()

    async def bump_roles_version_if_active(self, cid: UUID, user_id: UUID) -> int | None:
        """After a password reset (§6.5): the new ``roles_version``; ``None`` unless active."""
        stmt = (
            update(User)
            .where(User.company_id == cid, User.id == user_id, User.status == "active")
            .values(roles_version=User.roles_version + 1, updated_at=func.now())
            .returning(User.roles_version)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def activate_pending(self, cid: UUID, user_id: UUID) -> User | None:
        stmt = (
            update(User)
            .where(User.company_id == cid, User.id == user_id, User.status == "pending")
            .values(status="active", activated_at=func.now(), updated_at=func.now())
            .returning(User)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()


class CredentialRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def insert(self, *, user_id: UUID, cid: UUID, password_hash: str) -> None:
        await self._s.execute(
            insert(Credential).values(user_id=user_id, company_id=cid, password_hash=password_hash)
        )

    async def set_password(self, *, user_id: UUID, cid: UUID, password_hash: str) -> None:
        """Insert or replace the credential; ``password_changed_at = now()`` (reset, §6.6)."""
        stmt = pg_insert(Credential).values(
            user_id=user_id, company_id=cid, password_hash=password_hash
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[Credential.user_id],
            set_={
                "password_hash": stmt.excluded.password_hash,
                "password_changed_at": func.now(),
                "updated_at": func.now(),
            },
        )
        await self._s.execute(stmt)

    async def rehash(self, *, user_id: UUID, cid: UUID, password_hash: str) -> None:
        """Store a hash recomputed with new parameters (``password_changed_at`` is unchanged)."""
        await self._s.execute(
            update(Credential)
            .where(Credential.user_id == user_id, Credential.company_id == cid)
            .values(password_hash=password_hash, updated_at=func.now())
        )


@dataclass(frozen=True)
class InviteForAccept:
    invite_id: UUID
    user_id: UUID
    invite_status: str
    unexpired: bool
    user_status: str


@dataclass(frozen=True)
class IssuedFor:
    """What the relay needs after issuing a token: the new expiry and the recipient's name."""

    expires_at: datetime
    name: str


class InviteRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def insert(self, *, cid: UUID, user_id: UUID, created_by: UUID | None) -> UUID:
        invite_id = new_id()
        await self._s.execute(
            insert(Invite).values(
                id=invite_id, company_id=cid, user_id=user_id, created_by=created_by
            )
        )
        return invite_id

    async def get(self, cid: UUID, invite_id: UUID) -> Invite | None:
        stmt = select(Invite).where(Invite.company_id == cid, Invite.id == invite_id)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def revoke_pending_for_user(self, cid: UUID, user_id: UUID) -> UUID | None:
        """Revoke the user's pending invite (at most one, ``uq_invites_user_pending``)."""
        stmt = (
            update(Invite)
            .where(Invite.company_id == cid, Invite.user_id == user_id, Invite.status == "pending")
            .values(status="revoked", revoked_at=func.now(), updated_at=func.now())
            .returning(Invite.id)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def pending_ids_for(self, cid: UUID, user_ids: Sequence[UUID]) -> dict[UUID, UUID]:
        """``{user_id: pending invite id}`` for the given users (one query per list page)."""
        if not user_ids:
            return {}
        stmt = select(Invite.user_id, Invite.id).where(
            Invite.company_id == cid,
            Invite.user_id.in_(list(user_ids)),
            Invite.status == "pending",
        )
        return {row[0]: row[1] for row in await self._s.execute(stmt)}

    async def pending_id_for(self, cid: UUID, user_id: UUID) -> UUID | None:
        stmt = select(Invite.id).where(
            Invite.company_id == cid, Invite.user_id == user_id, Invite.status == "pending"
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def find_for_accept(self, cid: UUID, digest: bytes) -> InviteForAccept | None:
        stmt = (
            select(
                Invite.id,
                Invite.user_id,
                Invite.status,
                func.coalesce(Invite.expires_at > func.now(), False),
                User.status,
            )
            .join(User, User.id == Invite.user_id)
            .where(Invite.company_id == cid, Invite.token_hash == digest)
        )
        row = (await self._s.execute(stmt)).first()
        if row is None:
            return None
        return InviteForAccept(row[0], row[1], row[2], bool(row[3]), row[4])

    async def mark_accepted(self, cid: UUID, invite_id: UUID) -> UUID | None:
        """Single use: only a pending, unexpired invite moves to ``accepted`` (§6.6)."""
        stmt = (
            update(Invite)
            .where(
                Invite.company_id == cid,
                Invite.id == invite_id,
                Invite.status == "pending",
                Invite.expires_at > func.now(),
            )
            .values(status="accepted", accepted_at=func.now(), updated_at=func.now())
            .returning(Invite.user_id)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def issue_token(self, invite_id: UUID, digest: bytes, ttl_h: int) -> IssuedFor | None:
        """Replace the token and restart the TTL from now (R17, C31); ``None`` if not issuable."""
        stmt = text(
            """
            UPDATE identity.invites AS i
               SET token_hash = :digest,
                   token_issued_at = now(),
                   expires_at = now() + make_interval(hours => :ttl_h),
                   updated_at = now()
              FROM identity.users AS u
             WHERE i.id = :id AND i.status = 'pending'
               AND u.id = i.user_id AND u.status = 'pending'
         RETURNING i.expires_at, u.name
            """
        )
        row = (
            await self._s.execute(stmt, {"digest": digest, "ttl_h": ttl_h, "id": invite_id})
        ).first()
        return None if row is None else IssuedFor(expires_at=row[0], name=row[1])


@dataclass(frozen=True)
class ResetForConfirm:
    reset_id: UUID
    user_id: UUID
    reset_status: str
    unexpired: bool
    user_status: str


class ResetRepo:
    """Password resets: request and confirm (§6.6), and the relay's token issue (§6.7)."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def insert(self, *, cid: UUID, user_id: UUID) -> UUID:
        reset_id = new_id()
        await self._s.execute(
            insert(PasswordReset).values(id=reset_id, company_id=cid, user_id=user_id)
        )
        return reset_id

    async def revoke_pending_for_user(self, cid: UUID, user_id: UUID) -> UUID | None:
        """Revoke the user's pending reset (at most one, ``uq_password_resets_user_pending``)."""
        stmt = (
            update(PasswordReset)
            .where(
                PasswordReset.company_id == cid,
                PasswordReset.user_id == user_id,
                PasswordReset.status == "pending",
            )
            .values(status="revoked", revoked_at=func.now(), updated_at=func.now())
            .returning(PasswordReset.id)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def find_for_confirm(self, cid: UUID, digest: bytes) -> ResetForConfirm | None:
        stmt = (
            select(
                PasswordReset.id,
                PasswordReset.user_id,
                PasswordReset.status,
                func.coalesce(PasswordReset.expires_at > func.now(), False),
                User.status,
            )
            .join(User, User.id == PasswordReset.user_id)
            .where(PasswordReset.company_id == cid, PasswordReset.token_hash == digest)
        )
        row = (await self._s.execute(stmt)).first()
        if row is None:
            return None
        return ResetForConfirm(row[0], row[1], row[2], bool(row[3]), row[4])

    async def mark_used(self, cid: UUID, reset_id: UUID) -> UUID | None:
        """Single use: only a pending, unexpired reset moves to ``used`` (§6.6)."""
        stmt = (
            update(PasswordReset)
            .where(
                PasswordReset.company_id == cid,
                PasswordReset.id == reset_id,
                PasswordReset.status == "pending",
                PasswordReset.expires_at > func.now(),
            )
            .values(status="used", used_at=func.now(), updated_at=func.now())
            .returning(PasswordReset.user_id)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def issue_token(self, reset_id: UUID, digest: bytes, ttl_h: int) -> IssuedFor | None:
        stmt = text(
            """
            UPDATE identity.password_resets AS r
               SET token_hash = :digest,
                   token_issued_at = now(),
                   expires_at = now() + make_interval(hours => :ttl_h),
                   updated_at = now()
              FROM identity.users AS u
             WHERE r.id = :id AND r.status = 'pending'
               AND u.id = r.user_id AND u.status = 'active'
         RETURNING r.expires_at, u.name
            """
        )
        row = (
            await self._s.execute(stmt, {"digest": digest, "ttl_h": ttl_h, "id": reset_id})
        ).first()
        return None if row is None else IssuedFor(expires_at=row[0], name=row[1])


class AuditRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def append(
        self,
        *,
        cid: UUID,
        event: str,
        actor_user_id: UUID | None = None,
        target_user_id: UUID | None = None,
        ip_hash: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> UUID:
        audit_id = new_id()
        await self._s.execute(
            insert(AuditLog).values(
                id=audit_id,
                company_id=cid,
                event=event,
                actor_user_id=actor_user_id,
                target_user_id=target_user_id,
                ip_hash=ip_hash,
                details=dict(details or {}),
            )
        )
        return audit_id

    async def list_page(self, cid: UUID, *, before: UUID | None, limit: int) -> list[AuditLog]:
        """Newest first (``id DESC``; UUIDv7, so time order); ``limit + 1`` rows."""
        stmt = select(AuditLog).where(AuditLog.company_id == cid)
        if before is not None:
            stmt = stmt.where(AuditLog.id < before)
        stmt = stmt.order_by(AuditLog.id.desc()).limit(limit + 1)
        return list((await self._s.execute(stmt)).scalars())


@dataclass(frozen=True)
class ClaimedEmail:
    id: UUID
    company_id: UUID
    template: str
    recipient: str
    invite_id: UUID | None
    password_reset_id: UUID | None
    attempts: int
    traceparent: str | None


class OutboxRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def queue(
        self,
        *,
        cid: UUID,
        template: str,
        recipient: str,
        invite_id: UUID | None = None,
        password_reset_id: UUID | None = None,
        traceparent: str | None = None,
    ) -> UUID:
        email_id = new_id()
        await self._s.execute(
            insert(EmailOutbox).values(
                id=email_id,
                company_id=cid,
                template=template,
                recipient=recipient,
                invite_id=invite_id,
                password_reset_id=password_reset_id,
                traceparent=traceparent,
            )
        )
        return email_id

    async def cancel_pending(
        self, *, invite_id: UUID | None = None, password_reset_id: UUID | None = None
    ) -> int:
        """Cancel the pending rows of a revoked invite or reset (§6.5, §6.6)."""
        if invite_id is not None:
            ref = EmailOutbox.invite_id == invite_id
        elif password_reset_id is not None:
            ref = EmailOutbox.password_reset_id == password_reset_id
        else:
            return 0
        result = await self._s.execute(
            update(EmailOutbox)
            .where(ref, EmailOutbox.status == "pending")
            .values(status="cancelled", finished_at=func.now(), updated_at=func.now())
            .returning(EmailOutbox.id)
        )
        return len(result.all())

    async def claim(self, *, batch: int, lease_s: int) -> list[ClaimedEmail]:
        """Claim due rows with ``SKIP LOCKED``; the lease is ``next_attempt_at`` (§6.9)."""
        stmt = text(
            """
            WITH due AS (
                SELECT id FROM identity.email_outbox
                 WHERE status = 'pending' AND next_attempt_at <= now()
                 ORDER BY next_attempt_at
                 LIMIT :batch
                   FOR UPDATE SKIP LOCKED
            )
            UPDATE identity.email_outbox AS o
               SET attempts = o.attempts + 1,
                   next_attempt_at = now() + make_interval(secs => :lease_s),
                   updated_at = now()
              FROM due
             WHERE o.id = due.id
         RETURNING o.id, o.company_id, o.template, o.recipient, o.invite_id,
                   o.password_reset_id, o.attempts, o.traceparent
            """
        )
        rows = (await self._s.execute(stmt, {"batch": batch, "lease_s": float(lease_s)})).all()
        claimed = [ClaimedEmail(*row) for row in rows]
        return sorted(claimed, key=lambda c: c.id)

    async def finish(self, email_id: UUID, status: str, *, last_error: str | None = None) -> bool:
        """Record the outcome of a claimed row; False if it is no longer ``pending``.

        A resend, deactivation or repeat reset request may cancel the row while the relay is
        sending it (§6.9); the cancellation stands and the outcome is dropped.
        """
        result = await self._s.execute(
            update(EmailOutbox)
            .where(EmailOutbox.id == email_id, EmailOutbox.status == "pending")
            .values(
                status=status,
                finished_at=func.now(),
                last_error=last_error,
                updated_at=func.now(),
            )
            .returning(EmailOutbox.id)
        )
        return len(result.all()) == 1

    async def retry_in(self, email_id: UUID, delay_s: float, *, last_error: str) -> None:
        await self._s.execute(
            update(EmailOutbox)
            .where(EmailOutbox.id == email_id, EmailOutbox.status == "pending")
            .values(
                next_attempt_at=func.now()
                + text("make_interval(secs => :d)").bindparams(d=float(delay_s)),
                last_error=last_error,
                updated_at=func.now(),
            )
        )

    async def release_uncounted(self, email_id: UUID, delay_s: float) -> None:
        """The breaker was open: undo the claim's attempt and come back after ``delay_s``."""
        await self._s.execute(
            update(EmailOutbox)
            .where(EmailOutbox.id == email_id, EmailOutbox.status == "pending")
            .values(
                attempts=EmailOutbox.attempts - 1,
                next_attempt_at=func.now()
                + text("make_interval(secs => :d)").bindparams(d=float(delay_s)),
                last_error="breaker_open",
                updated_at=func.now(),
            )
        )

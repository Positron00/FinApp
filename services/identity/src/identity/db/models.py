"""SQLAlchemy models for schema ``identity`` (identity LLD §2; PAY-606).

The migration ``0001_initial`` creates the same tables, constraints and indexes by hand;
``tests/integration/test_migrations.py`` checks that the two agree.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final
from uuid import UUID

from pycommon.db import TimestampMixin
from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "NAMING_CONVENTION",
    "AuditLog",
    "Base",
    "Credential",
    "EmailOutbox",
    "Invite",
    "PasswordReset",
    "User",
]

NAMING_CONVENTION: Final = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}

ROLES_SQL: Final = "ARRAY['admin','payroll_admin','approver']::text[]"
AUDIT_EVENTS_SQL: Final = (
    "'user.bootstrapped','user.invited','user.invite_resent','user.invite_accepted',"
    "'auth.signin.succeeded','auth.signin.failed','auth.lockout',"
    "'auth.password_reset.requested','auth.password_reset.completed',"
    "'user.roles_changed','user.deactivated'"
)


class Base(DeclarativeBase):
    metadata = MetaData(schema="identity", naming_convention=NAMING_CONVENTION)


def _ts(nullable: bool = True) -> Any:
    return mapped_column(DateTime(timezone=True), nullable=nullable)


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            f"cardinality(roles) BETWEEN 1 AND 3 AND roles <@ {ROLES_SQL}", name="roles"
        ),
        CheckConstraint("roles_version >= 1", name="roles_version"),
        CheckConstraint("status IN ('pending','active','inactive')", name="status"),
        Index("uq_users_company_email", "company_id", text("lower(email)"), unique=True),
        Index("ix_users_company_id_id", "company_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    roles_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    activated_at: Mapped[datetime | None] = _ts()
    deactivated_at: Mapped[datetime | None] = _ts()
    created_by: Mapped[UUID | None] = mapped_column(nullable=True)


class Credential(TimestampMixin, Base):
    __tablename__ = "credentials"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("identity.users.id", ondelete="CASCADE"), primary_key=True
    )
    company_id: Mapped[UUID] = mapped_column(nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Invite(TimestampMixin, Base):
    __tablename__ = "invites"
    __table_args__ = (
        CheckConstraint(
            "token_hash IS NULL OR octet_length(token_hash) = 32", name="token_hash_len"
        ),
        CheckConstraint("status IN ('pending','accepted','revoked')", name="status"),
        CheckConstraint("token_hash IS NULL OR expires_at IS NOT NULL", name="expiry"),
        Index(
            "uq_invites_token_hash",
            "token_hash",
            unique=True,
            postgresql_where=text("token_hash IS NOT NULL"),
        ),
        Index(
            "uq_invites_user_pending",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_invites_expires_at", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("identity.users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_issued_at: Mapped[datetime | None] = _ts()
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    expires_at: Mapped[datetime | None] = _ts()
    created_by: Mapped[UUID | None] = mapped_column(nullable=True)
    accepted_at: Mapped[datetime | None] = _ts()
    revoked_at: Mapped[datetime | None] = _ts()


class PasswordReset(TimestampMixin, Base):
    __tablename__ = "password_resets"
    __table_args__ = (
        CheckConstraint(
            "token_hash IS NULL OR octet_length(token_hash) = 32", name="token_hash_len"
        ),
        CheckConstraint("status IN ('pending','used','revoked')", name="status"),
        CheckConstraint("token_hash IS NULL OR expires_at IS NOT NULL", name="expiry"),
        Index(
            "uq_password_resets_token_hash",
            "token_hash",
            unique=True,
            postgresql_where=text("token_hash IS NOT NULL"),
        ),
        Index(
            "uq_password_resets_user_pending",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_password_resets_expires_at", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("identity.users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_issued_at: Mapped[datetime | None] = _ts()
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    expires_at: Mapped[datetime | None] = _ts()
    used_at: Mapped[datetime | None] = _ts()
    revoked_at: Mapped[datetime | None] = _ts()


class AuditLog(TimestampMixin, Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(f"event IN ({AUDIT_EVENTS_SQL})", name="event"),
        Index("ix_audit_log_company_id_id", "company_id", text("id DESC")),
        Index("ix_audit_log_at", "at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(nullable=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    target_user_id: Mapped[UUID | None] = mapped_column(nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class EmailOutbox(TimestampMixin, Base):
    __tablename__ = "email_outbox"
    __table_args__ = (
        CheckConstraint("template IN ('invite','password_reset')", name="template"),
        CheckConstraint("status IN ('pending','sent','failed','cancelled')", name="status"),
        CheckConstraint("attempts >= 0", name="attempts"),
        CheckConstraint(
            "(template = 'invite' AND invite_id IS NOT NULL AND password_reset_id IS NULL) OR "
            "(template = 'password_reset' AND password_reset_id IS NOT NULL "
            "AND invite_id IS NULL)",
            name="ref",
        ),
        Index(
            "ix_email_outbox_due", "next_attempt_at", postgresql_where=text("status = 'pending'")
        ),
        Index(
            "ix_email_outbox_finished_at",
            "finished_at",
            postgresql_where=text("status <> 'pending'"),
        ),
        Index("ix_email_outbox_invite_id", "invite_id"),
        Index("ix_email_outbox_password_reset_id", "password_reset_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False)
    template: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient: Mapped[str] = mapped_column(String(254), nullable=False)
    invite_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("identity.invites.id", ondelete="CASCADE"), nullable=True
    )
    password_reset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("identity.password_resets.id", ondelete="CASCADE"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    traceparent: Mapped[str | None] = mapped_column(String(55), nullable=True)
    finished_at: Mapped[datetime | None] = _ts()

"""Initial identity schema (identity LLD §2; PAY-606, C2, C50).

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "identity"
ROLES_SQL = "ARRAY['admin','payroll_admin','approver']::text[]"
AUDIT_EVENTS_SQL = (
    "'user.bootstrapped','user.invited','user.invite_resent','user.invite_accepted',"
    "'auth.signin.succeeded','auth.signin.failed','auth.lockout',"
    "'auth.password_reset.requested','auth.password_reset.completed',"
    "'user.roles_changed','user.deactivated'"
)

PURGE_AUDIT_FN = """
CREATE FUNCTION identity.purge_audit_log(cutoff timestamptz) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = identity, pg_temp
AS $$
DECLARE
  n integer;
BEGIN
  DELETE FROM identity.audit_log WHERE at < cutoff;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END
$$
"""


def _ts(name: str, *, nullable: bool = True, default_now: bool = False) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if default_now else None,
    )


def _stamps() -> list[sa.Column[object]]:
    return [
        _ts("created_at", nullable=False, default_now=True),
        _ts("updated_at", nullable=False, default_now=True),
    ]


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("roles", pg.ARRAY(sa.Text()), nullable=False),
        sa.Column("roles_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        _ts("activated_at"),
        _ts("deactivated_at"),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.CheckConstraint(
            f"cardinality(roles) BETWEEN 1 AND 3 AND roles <@ {ROLES_SQL}",
            name=op.f("ck_users_roles"),
        ),
        sa.CheckConstraint("roles_version >= 1", name=op.f("ck_users_roles_version")),
        sa.CheckConstraint(
            "status IN ('pending','active','inactive')", name=op.f("ck_users_status")
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_users_company_email",
        "users",
        ["company_id", sa.text("lower(email)")],
        unique=True,
        schema=SCHEMA,
    )
    op.create_index("ix_users_company_id_id", "users", ["company_id", "id"], schema=SCHEMA)

    op.create_table(
        "credentials",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        _ts("password_changed_at", nullable=False, default_now=True),
        *_stamps(),
        sa.PrimaryKeyConstraint("user_id", name="pk_credentials"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["identity.users.id"],
            name="fk_credentials_user_id",
            ondelete="CASCADE",
        ),
        schema=SCHEMA,
    )

    for table, statuses, extra in (
        ("invites", "'pending','accepted','revoked'", ["created_by", "accepted_at"]),
        ("password_resets", "'pending','used','revoked'", ["used_at"]),
    ):
        extra_cols: list[sa.Column[object]] = []
        if "created_by" in extra:
            extra_cols.append(sa.Column("created_by", sa.Uuid(), nullable=True))
        extra_cols.extend(_ts(c) for c in extra if c != "created_by")
        op.create_table(
            table,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("company_id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("token_hash", sa.LargeBinary(), nullable=True),
            _ts("token_issued_at"),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            _ts("expires_at"),
            *extra_cols,
            _ts("revoked_at"),
            *_stamps(),
            sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
            sa.ForeignKeyConstraint(
                ["user_id"], ["identity.users.id"], name=f"fk_{table}_user_id", ondelete="CASCADE"
            ),
            sa.CheckConstraint(
                "token_hash IS NULL OR octet_length(token_hash) = 32",
                name=op.f(f"ck_{table}_token_hash_len"),
            ),
            sa.CheckConstraint(f"status IN ({statuses})", name=op.f(f"ck_{table}_status")),
            sa.CheckConstraint(
                "token_hash IS NULL OR expires_at IS NOT NULL", name=op.f(f"ck_{table}_expiry")
            ),
            schema=SCHEMA,
        )
        op.create_index(
            f"uq_{table}_token_hash",
            table,
            ["token_hash"],
            unique=True,
            postgresql_where=sa.text("token_hash IS NOT NULL"),
            schema=SCHEMA,
        )
        op.create_index(
            f"uq_{table}_user_pending",
            table,
            ["user_id"],
            unique=True,
            postgresql_where=sa.text("status = 'pending'"),
            schema=SCHEMA,
        )
        op.create_index(f"ix_{table}_expires_at", table, ["expires_at"], schema=SCHEMA)

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        _ts("at", nullable=False, default_now=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=True),
        sa.Column("ip_hash", sa.CHAR(64), nullable=True),
        sa.Column("details", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
        sa.CheckConstraint(f"event IN ({AUDIT_EVENTS_SQL})", name=op.f("ck_audit_log_event")),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_audit_log_company_id_id",
        "audit_log",
        ["company_id", sa.text("id DESC")],
        schema=SCHEMA,
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"], schema=SCHEMA)

    op.create_table(
        "email_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("template", sa.String(32), nullable=False),
        sa.Column("recipient", sa.String(254), nullable=False),
        sa.Column("invite_id", sa.Uuid(), nullable=True),
        sa.Column("password_reset_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        _ts("next_attempt_at", nullable=False, default_now=True),
        sa.Column("last_error", sa.String(64), nullable=True),
        sa.Column("traceparent", sa.String(55), nullable=True),
        _ts("finished_at"),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name="pk_email_outbox"),
        sa.ForeignKeyConstraint(
            ["invite_id"],
            ["identity.invites.id"],
            name="fk_email_outbox_invite_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["password_reset_id"],
            ["identity.password_resets.id"],
            name="fk_email_outbox_password_reset_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "template IN ('invite','password_reset')", name=op.f("ck_email_outbox_template")
        ),
        sa.CheckConstraint(
            "status IN ('pending','sent','failed','cancelled')", name=op.f("ck_email_outbox_status")
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_email_outbox_attempts")),
        sa.CheckConstraint(
            "(template = 'invite' AND invite_id IS NOT NULL AND password_reset_id IS NULL) OR "
            "(template = 'password_reset' AND password_reset_id IS NOT NULL "
            "AND invite_id IS NULL)",
            name=op.f("ck_email_outbox_ref"),
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_email_outbox_due",
        "email_outbox",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_email_outbox_finished_at",
        "email_outbox",
        ["finished_at"],
        postgresql_where=sa.text("status <> 'pending'"),
        schema=SCHEMA,
    )
    op.create_index("ix_email_outbox_invite_id", "email_outbox", ["invite_id"], schema=SCHEMA)
    op.create_index(
        "ix_email_outbox_password_reset_id", "email_outbox", ["password_reset_id"], schema=SCHEMA
    )

    # C2, C50: the runtime role may append to the audit log but never rewrite or delete it.
    op.execute("REVOKE UPDATE, DELETE ON identity.audit_log FROM identity_app")
    # Retention purging goes through this definer-rights function (owned by identity_migrator).
    op.execute(PURGE_AUDIT_FN)
    op.execute("REVOKE ALL ON FUNCTION identity.purge_audit_log(timestamptz) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION identity.purge_audit_log(timestamptz) TO identity_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS identity.purge_audit_log(timestamptz)")
    for table in (
        "email_outbox",
        "audit_log",
        "password_resets",
        "invites",
        "credentials",
        "users",
    ):
        op.drop_table(table, schema=SCHEMA)

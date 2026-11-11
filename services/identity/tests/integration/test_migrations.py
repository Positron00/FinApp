"""Migrations: up, down one step, up again (identity LLD §2.9, §7.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from identity.paths import MIGRATIONS_DIR
from pycommon.testing import PgBootstrapped

SERVICE_DIR = MIGRATIONS_DIR.parent
EXPECTED_CONSTRAINTS = {
    "pk_users", "ck_users_roles", "ck_users_roles_version", "ck_users_status",
    "pk_credentials", "fk_credentials_user_id",
    "pk_invites", "fk_invites_user_id", "ck_invites_token_hash_len", "ck_invites_status",
    "ck_invites_expiry",
    "pk_password_resets", "fk_password_resets_user_id", "ck_password_resets_token_hash_len",
    "ck_password_resets_status", "ck_password_resets_expiry",
    "pk_audit_log", "ck_audit_log_event",
    "pk_email_outbox", "fk_email_outbox_invite_id", "fk_email_outbox_password_reset_id",
    "ck_email_outbox_template", "ck_email_outbox_status", "ck_email_outbox_attempts",
    "ck_email_outbox_ref",
}  # fmt: skip
EXPECTED_INDEXES = {
    "uq_users_company_email", "ix_users_company_id_id",
    "uq_invites_token_hash", "uq_invites_user_pending", "ix_invites_expires_at",
    "uq_password_resets_token_hash", "uq_password_resets_user_pending",
    "ix_password_resets_expires_at",
    "ix_audit_log_company_id_id", "ix_audit_log_at",
    "ix_email_outbox_due", "ix_email_outbox_finished_at", "ix_email_outbox_invite_id",
    "ix_email_outbox_password_reset_id",
}  # fmt: skip


def _alembic(pg: PgBootstrapped, monkeypatch: pytest.MonkeyPatch, fn: str, rev: str) -> None:
    for k, v in pg.env_for("identity_migrator").items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(SERVICE_DIR)
    getattr(command, fn)(Config(str(Path(SERVICE_DIR) / "alembic.ini")), rev)


async def _names(admin_db: Any) -> tuple[set[str], set[str], set[str]]:
    cons = await admin_db.fetch(
        "SELECT conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
        "WHERE n.nspname = 'identity'"
    )
    idx = await admin_db.fetch("SELECT indexname FROM pg_indexes WHERE schemaname = 'identity'")
    tables = await admin_db.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'identity'")
    return (
        {r[0] for r in cons},
        {r[0] for r in idx},
        {r[0] for r in tables},
    )


async def test_upgrade_downgrade_upgrade(
    migrated: PgBootstrapped, admin_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAY-606: head → down one step → head; alembic_version lives in schema identity."""
    head = ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()
    assert head == "0001_initial"
    cons, idx, tables = await _names(admin_db)
    assert cons >= EXPECTED_CONSTRAINTS
    assert idx >= EXPECTED_INDEXES
    assert {
        "users",
        "credentials",
        "invites",
        "password_resets",
        "audit_log",
        "email_outbox",
    } <= tables
    assert await admin_db.fetchval("SELECT version_num FROM identity.alembic_version") == head
    assert await admin_db.fetchval("SELECT to_regclass('public.alembic_version')") is None

    _alembic(migrated, monkeypatch, "downgrade", "-1")
    cons, idx, tables = await _names(admin_db)
    assert tables == {"alembic_version"}
    assert await admin_db.fetchval("SELECT count(*) FROM identity.alembic_version") == 0
    assert (
        await admin_db.fetchval("SELECT to_regprocedure('identity.purge_audit_log(timestamptz)')")
        is None
    )

    _alembic(migrated, monkeypatch, "upgrade", "head")
    cons, idx, tables = await _names(admin_db)
    assert cons >= EXPECTED_CONSTRAINTS
    assert idx >= EXPECTED_INDEXES
    assert await admin_db.fetchval("SELECT version_num FROM identity.alembic_version") == head
    owner = await admin_db.fetchval(
        "SELECT pg_get_userbyid(proowner) FROM pg_proc "
        "WHERE oid = 'identity.purge_audit_log(timestamptz)'::regprocedure"
    )
    assert owner == "identity_migrator"


async def test_models_match_the_migration(migrated: PgBootstrapped) -> None:
    """PAY-606: autogenerate finds no difference between the models and the migrated schema."""
    from alembic.autogenerate import compare_metadata  # noqa: PLC0415
    from alembic.migration import MigrationContext  # noqa: PLC0415
    from identity.db.models import Base  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    engine = create_async_engine(migrated.url_for("postgres"))

    def diff(conn: Any) -> list[Any]:
        ctx = MigrationContext.configure(
            conn,
            opts={
                "include_schemas": True,
                "include_name": lambda name, type_, _p: type_ != "schema" or name == "identity",
                "version_table_schema": "identity",
            },
        )
        return list(compare_metadata(ctx, Base.metadata))

    try:
        async with engine.connect() as conn:
            changes = await conn.run_sync(diff)
    finally:
        await engine.dispose()
    assert changes == []

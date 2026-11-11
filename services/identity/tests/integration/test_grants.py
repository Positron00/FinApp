"""DB grants for identity's roles (identity LLD §2.1, §2.6, §7.2; C2, C50)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest
from pycommon.testing import PgBootstrapped

from tests.helpers import COMPANY_ID
from tests.integration.conftest import connect

OTHER_SCHEMAS = ("payroll", "payouts", "bank_stub")


def rerun_bootstrap(pg: PgBootstrapped) -> None:
    """Re-run the identity bootstrap SQL in the container, as a password rotation would."""
    result = pg.container.exec(
        [
            "psql", "-v", "ON_ERROR_STOP=1", "-X", "-q", "-U", "postgres", "-d", "payrun",
            "-f", "/docker-entrypoint-initdb.d/10-identity.sql",
        ]
    )  # fmt: skip
    assert result.exit_code == 0, result.output.decode(errors="replace")


async def _insert_audit(conn: Any, at_sql: str = "now()") -> None:
    await conn.execute(
        "INSERT INTO identity.audit_log (id, company_id, event, at) "
        f"VALUES ($1, $2, 'user.invited', {at_sql})",
        uuid4(),
        COMPANY_ID,
    )


async def _denied(conn: Any, sql: str, *args: Any) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
        await conn.execute(sql, *args)


async def _assert_audit_log_append_only(conn: Any) -> None:
    await _insert_audit(conn)
    await _denied(conn, "UPDATE identity.audit_log SET event = 'user.deactivated'")
    await _denied(conn, "DELETE FROM identity.audit_log")


async def test_runtime_role_grants(migrated: PgBootstrapped) -> None:
    """PAY-606, C2, C50: identity_app can do DML on its tables, append-only on audit_log, and
    nothing in other schemas, on DDL or on alembic_version."""
    conn = await connect(migrated, "identity_app")
    try:
        for schema in OTHER_SCHEMAS:
            await _denied(conn, f"CREATE TABLE {schema}.x (id int)")
            await _denied(conn, f"SELECT * FROM {schema}.anything")
        await _denied(conn, "CREATE TABLE identity.x (id int)")
        await _denied(conn, "INSERT INTO identity.alembic_version VALUES ('x')")
        await _denied(conn, "UPDATE identity.alembic_version SET version_num = 'x'")
        await _assert_audit_log_append_only(conn)
        for table in ("users", "credentials", "invites", "password_resets", "email_outbox"):
            await conn.execute(f"SELECT count(*) FROM identity.{table}")
            await conn.execute(f"DELETE FROM identity.{table} WHERE false")
            await conn.execute(f"UPDATE identity.{table} SET updated_at = now() WHERE false")
    finally:
        await conn.close()


async def test_audit_log_stays_append_only_after_bootstrap_rerun(
    migrated: PgBootstrapped,
) -> None:
    """C50: re-running 10-identity.sql (whose template re-grants DML) keeps UPDATE and DELETE
    on identity.audit_log revoked."""
    rerun_bootstrap(migrated)
    conn = await connect(migrated, "identity_app")
    try:
        await _assert_audit_log_append_only(conn)
        # The re-run's blanket grant does restore DML on the other tables.
        await conn.execute("DELETE FROM identity.users WHERE false")
    finally:
        await conn.close()


async def test_purge_audit_log_deletes_only_older_rows(migrated: PgBootstrapped) -> None:
    """PAY-NFR-08, C50: identity.purge_audit_log(cutoff) (SECURITY DEFINER) removes rows with
    at < cutoff and nothing else; identity_app may execute it."""
    conn = await connect(migrated, "identity_app")
    try:
        await _insert_audit(conn, "now() - interval '401 days'")
        await _insert_audit(conn, "now() - interval '402 days'")
        await _insert_audit(conn, "now() - interval '10 days'")
        deleted = await conn.fetchval(
            "SELECT identity.purge_audit_log(now() - interval '400 days')"
        )
        assert deleted == 2
        assert await conn.fetchval("SELECT count(*) FROM identity.audit_log") == 1
    finally:
        await conn.close()


async def test_purge_function_is_not_public(admin_db: Any) -> None:
    """C50: only identity_app (and the owner) can execute the purge function."""
    acl = await admin_db.fetchval(
        "SELECT proacl::text FROM pg_proc "
        "WHERE oid = 'identity.purge_audit_log(timestamptz)'::regprocedure"
    )
    assert "identity_app=X" in acl
    assert "=X/" not in acl.replace("identity_app=X/", "").replace("identity_migrator=X/", "")
    definer = await admin_db.fetchval(
        "SELECT prosecdef FROM pg_proc "
        "WHERE oid = 'identity.purge_audit_log(timestamptz)'::regprocedure"
    )
    assert definer is True


async def test_migrator_has_no_rights_elsewhere(migrated: PgBootstrapped) -> None:
    """PAY-606: identity_migrator can't touch the other services' schemas."""
    conn = await connect(migrated, "identity_migrator")
    try:
        for schema in OTHER_SCHEMAS:
            await _denied(conn, f"CREATE TABLE {schema}.x (id int)")
            await _denied(conn, f"SELECT * FROM {schema}.anything")
    finally:
        await conn.close()

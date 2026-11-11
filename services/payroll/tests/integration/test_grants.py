"""Runtime-role grants (PAY-606): DML in payroll only; alembic_version read-only."""

from __future__ import annotations

from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from pycommon.testing import PgBootstrapped

pytestmark = pytest.mark.integration


async def _app_conn(pg: PgBootstrapped) -> Any:
    e = pg.env_for("payroll_app")
    return await asyncpg.connect(
        host=e["DB_HOST"],
        port=int(e["DB_PORT"]),
        user=e["DB_USER"],
        password=e["DB_PASSWORD"],
        database="payrun",
    )


@pytest.mark.parametrize(
    "table", ["identity.users", "payouts.disbursements", "bank_stub.transfers", "payroll_x.nope"]
)
async def test_no_access_to_other_schemas(migrated: PgBootstrapped, table: str) -> None:
    """PAY-606: payroll_app can't read another service's schema (permission denied)."""
    conn = await _app_conn(migrated)
    try:
        with pytest.raises(
            (
                asyncpg.InsufficientPrivilegeError,
                asyncpg.UndefinedTableError,
                asyncpg.InvalidSchemaNameError,
            )
        ) as err:
            await conn.fetch(f"SELECT 1 FROM {table} LIMIT 1")
        if table != "payroll_x.nope":
            assert isinstance(
                err.value, asyncpg.InsufficientPrivilegeError | asyncpg.UndefinedTableError
            )
    finally:
        await conn.close()


async def test_schema_permission_denied_on_identity(migrated: PgBootstrapped) -> None:
    """PAY-606: the identity schema itself is closed to payroll_app."""
    conn = await _app_conn(migrated)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.fetch("SELECT 1 FROM identity.nothing_here")
    finally:
        await conn.close()


async def test_dml_but_no_ddl(migrated: PgBootstrapped, db: Any) -> None:
    """PAY-606: DML on payroll tables works; DDL is refused."""
    conn = await _app_conn(migrated)
    try:
        assert await conn.fetchval("SELECT count(*) FROM payroll.company_settings") == 1
        await conn.execute("UPDATE payroll.company_settings SET name = name")
        for ddl in (
            "CREATE TABLE payroll.evil (id int)",
            "ALTER TABLE payroll.employees ADD COLUMN evil int",
            "DROP TABLE payroll.outbox",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(ddl)
    finally:
        await conn.close()


async def test_alembic_version_is_read_only(migrated: PgBootstrapped) -> None:
    """PAY-606: payroll_app can read but not write payroll.alembic_version."""
    conn = await _app_conn(migrated)
    try:
        assert await conn.fetchval("SELECT version_num FROM payroll.alembic_version")
        for sql in (
            "UPDATE payroll.alembic_version SET version_num = version_num",
            "DELETE FROM payroll.alembic_version",
            "INSERT INTO payroll.alembic_version VALUES ('x')",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(sql)
    finally:
        await conn.close()

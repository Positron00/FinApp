"""DB grants for the payouts roles (design §2.1; PAY-606, C2)."""

from __future__ import annotations

import asyncpg  # type: ignore[import-untyped]
import pytest
from pycommon.testing import PgBootstrapped

from tests.integration.conftest import connect

OTHER_SCHEMAS = ("payroll", "identity", "bank_stub")


async def test_runtime_role_grants(migrated: PgBootstrapped) -> None:
    """PAY-606: payouts_app has DML on its two tables, no DDL, read-only alembic_version and
    permission denied on payroll.*, identity.* and bank_stub.*."""
    conn = await connect(migrated, "payouts_app")
    try:
        for schema in OTHER_SCHEMAS:
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
                await conn.execute(f"SELECT * FROM {schema}.anything")
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
                await conn.execute(f"CREATE TABLE {schema}.x (id int)")
        for table in ("payroll.payouts", "bank_stub.transfers"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
                await conn.execute(f"SELECT count(*) FROM {table}")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("CREATE TABLE payouts.x (id int)")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("UPDATE payouts.alembic_version SET version_num = 'x'")
        for table in ("disbursements", "processed_events"):
            await conn.execute(f"SELECT count(*) FROM payouts.{table}")
            await conn.execute(f"DELETE FROM payouts.{table} WHERE false")
            await conn.execute(f"UPDATE payouts.{table} SET updated_at = now() WHERE false")
    finally:
        await conn.close()


async def test_migrator_has_no_rights_elsewhere(migrated: PgBootstrapped) -> None:
    """PAY-606: payouts_migrator can't touch the other services' schemas."""
    conn = await connect(migrated, "payouts_migrator")
    try:
        for schema in OTHER_SCHEMAS:
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
                await conn.execute(f"CREATE TABLE {schema}.x (id int)")
    finally:
        await conn.close()

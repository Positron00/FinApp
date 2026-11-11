"""DB grants for bank-stub's roles (design §2.1; PAY-606, C2)."""

from __future__ import annotations

import asyncpg  # type: ignore[import-untyped]
import pytest
from pycommon.testing import PgBootstrapped

from tests.integration.conftest import connect

OTHER_SCHEMAS = ("payroll", "payouts", "identity")


async def test_runtime_role_grants(migrated: PgBootstrapped) -> None:
    """PAY-606: bank_stub_app has DML on bank_stub.transfers, no DDL, read-only
    alembic_version, and permission denied in payroll, payouts and identity."""
    conn = await connect(migrated, "bank_stub_app")
    try:
        for schema in OTHER_SCHEMAS:
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
                await conn.execute(f"SELECT * FROM {schema}.anything")
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
                await conn.execute(f"CREATE TABLE {schema}.x (id int)")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("CREATE TABLE bank_stub.x (id int)")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("UPDATE bank_stub.alembic_version SET version_num = 'x'")
        await conn.execute("SELECT count(*) FROM bank_stub.transfers")
        await conn.execute("DELETE FROM bank_stub.transfers WHERE false")
        await conn.execute("UPDATE bank_stub.transfers SET updated_at = now() WHERE false")
    finally:
        await conn.close()


async def test_migrator_has_no_rights_elsewhere(migrated: PgBootstrapped) -> None:
    """PAY-606: bank_stub_migrator can't touch the other services' schemas."""
    conn = await connect(migrated, "bank_stub_migrator")
    try:
        for schema in OTHER_SCHEMAS:
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
                await conn.execute(f"CREATE TABLE {schema}.x (id int)")
    finally:
        await conn.close()

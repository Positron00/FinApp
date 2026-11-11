"""DB bootstrap SQL in deploy/compose/init (PAY-606, C2, C50; platform LLD §2.1)."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from pycommon.testing import PgBootstrapped

pytestmark = pytest.mark.integration

RUNTIME_ROLES = ("identity_app", "payroll_app", "payouts_app", "bank_stub_app")


def _run(pg: PgBootstrapped, role: str, sql: str) -> Any:
    async def go() -> Any:
        e = pg.env_for(role)
        conn = await asyncpg.connect(
            host=e["DB_HOST"],
            port=int(e["DB_PORT"]),
            user=e["DB_USER"],
            password=e["DB_PASSWORD"],
            database=e["DB_NAME"],
        )
        try:
            return await conn.fetchval(sql)
        finally:
            await conn.close()

    return asyncio.run(go())


@pytest.mark.parametrize("role", RUNTIME_ROLES)
def test_runtime_role_logs_in_with_its_schema_on_the_search_path(
    pg_bootstrapped: PgBootstrapped, role: str
) -> None:
    """PAY-606: a runtime role logs in with its password and sees only its schema."""
    assert _run(pg_bootstrapped, role, "show search_path") == role.removesuffix("_app")


@pytest.mark.parametrize("role", RUNTIME_ROLES)
def test_runtime_role_cannot_read_other_schemas_or_create_tables(
    pg_bootstrapped: PgBootstrapped, role: str
) -> None:
    """PAY-606: no cross-schema access and no DDL for runtime roles."""
    own = role.removesuffix("_app")
    for other in {"identity", "payroll", "payouts", "bank_stub"} - {own}:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            _run(pg_bootstrapped, role, f"select 1 from {other}.anything")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        _run(pg_bootstrapped, role, f"create table {own}.t_platform_test(i int)")


def test_wrong_password_is_refused(pg_bootstrapped: PgBootstrapped) -> None:
    """PAY-606: password authentication is enforced over the network."""

    async def go() -> None:
        await asyncpg.connect(
            host=pg_bootstrapped.host,
            port=pg_bootstrapped.port,
            user="payroll_app",
            password="wrong",
            database="payrun",
        )

    with pytest.raises(asyncpg.InvalidPasswordError):
        asyncio.run(go())


def test_public_has_no_connect_and_exporter_is_monitor_only(
    pg_bootstrapped: PgBootstrapped,
) -> None:
    """PAY-606: unknown roles can't connect; the exporter has pg_monitor and nothing else."""
    admin = pg_bootstrapped.admin_user
    assert (
        _run(pg_bootstrapped, admin, "select has_database_privilege('public','payrun','CONNECT')")
        is False
    )
    assert (
        _run(
            pg_bootstrapped, admin, "select pg_has_role('postgres_exporter','pg_monitor','member')"
        )
        is True
    )


def test_rerun_keeps_audit_log_append_only(pg_bootstrapped: PgBootstrapped) -> None:
    """PAY-606, C50: a bootstrap re-run never restores UPDATE/DELETE on the audit log."""
    pg = pg_bootstrapped
    _run(pg, "identity_migrator", "create table if not exists identity.audit_log(i int)")
    _run(pg, "identity_migrator", "revoke update, delete on identity.audit_log from identity_app")
    rerun = subprocess.run(
        [
            "docker",
            "exec",
            pg.container.get_wrapped_container().id,
            "sh",
            "-c",
            "cd /docker-entrypoint-initdb.d && psql -v ON_ERROR_STOP=1 -X -q "
            f"-U {pg.admin_user} -d payrun -f 10-identity.sql",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rerun.returncode == 0, rerun.stderr
    privileges = _run(
        pg,
        pg.admin_user,
        "select string_agg(privilege_type, ',' order by privilege_type) "
        "from information_schema.role_table_grants where table_schema='identity' "
        "and table_name='audit_log' and grantee='identity_app'",
    )
    assert privileges == "INSERT,SELECT"


def test_server_does_not_log_statements(pg_bootstrapped: PgBootstrapped) -> None:
    """PAY-NFR-07: passwords set by ALTER ROLE never reach the server log."""
    assert _run(pg_bootstrapped, pg_bootstrapped.admin_user, "show log_statement") == "none"

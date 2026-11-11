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


def _psql(
    pg: PgBootstrapped, sql_file: str, *, env: dict[str, str] | None = None, unset: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run one init file inside the Postgres container as the bootstrap admin."""
    prefix = f"env -u {unset} " if unset else ""
    flags = " ".join(f"-e {k}={v}" for k, v in (env or {}).items())
    cmd = (
        f"docker exec {flags} {pg.container.get_wrapped_container().id} sh -c "
        f"'cd /docker-entrypoint-initdb.d && {prefix}psql -v ON_ERROR_STOP=1 -X -q "
        f"-U {pg.admin_user} -d payrun -f {sql_file}'"
    )
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)  # noqa: S602


def test_each_migrator_owns_its_schema(pg_bootstrapped: PgBootstrapped) -> None:
    """PAY-606, C2: <schema>_migrator owns <schema>; public has no rights on it."""
    for schema in ("identity", "payroll", "payouts", "bank_stub"):
        owner = _run(
            pg_bootstrapped,
            pg_bootstrapped.admin_user,
            f"select nspowner::regrole::text from pg_namespace where nspname = '{schema}'",
        )
        assert owner == f"{schema}_migrator"
        assert (
            _run(
                pg_bootstrapped,
                pg_bootstrapped.admin_user,
                f"select has_schema_privilege('public', '{schema}', 'USAGE')",
            )
            is False
        )


def test_default_privileges_give_runtime_dml_on_migrator_tables(
    pg_bootstrapped: PgBootstrapped,
) -> None:
    """PAY-606: a table the migrator creates later is DML-able by the runtime role, no DDL."""
    pg = pg_bootstrapped
    _run(pg, "payouts_migrator", "create table if not exists payouts.t_defaults(i int)")
    assert _run(pg, "payouts_app", "insert into payouts.t_defaults values (1) returning i") == 1
    assert _run(pg, "payouts_app", "update payouts.t_defaults set i = 2 returning i") == 2
    assert _run(pg, "payouts_app", "delete from payouts.t_defaults returning i") == 2
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        _run(pg, "payouts_app", "drop table payouts.t_defaults")


def test_exporter_cannot_read_table_data(pg_bootstrapped: PgBootstrapped) -> None:
    """PAY-606: postgres_exporter reads stats views only, never table rows."""
    pg = pg_bootstrapped
    _run(pg, "payroll_migrator", "create table if not exists payroll.t_secret(i int)")
    assert _run(pg, "postgres_exporter", "select count(*) from pg_stat_database") >= 1
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        _run(pg, "postgres_exporter", "select * from payroll.t_secret")


@pytest.mark.parametrize("how", ["unset", "empty"])
def test_missing_password_fails_naming_the_variable(
    pg_bootstrapped: PgBootstrapped, how: str
) -> None:
    """PAY-606: an unset or empty password variable fails with its NAME, never a value."""
    if how == "unset":
        result = _psql(pg_bootstrapped, "20-payroll.sql", unset="PAYROLL_DB_PASSWORD")
    else:
        result = _psql(pg_bootstrapped, "20-payroll.sql", env={"PAYROLL_DB_PASSWORD": ""})
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "PAYROLL_DB_PASSWORD" in output
    assert "bootstrap: required password env var" in output
    for password in pg_bootstrapped.passwords.values():
        assert password not in output


def test_rerun_rotates_a_changed_password(pg_bootstrapped: PgBootstrapped) -> None:
    """PAY-606: re-running a file with a new password rotates it; the old one stops working."""
    pg = pg_bootstrapped
    original = pg.passwords["BANK_STUB_DB_PASSWORD"]
    rotated = original[::-1] + "R"
    try:
        result = _psql(pg, "40-bank-stub.sql", env={"BANK_STUB_DB_PASSWORD": rotated})
        assert result.returncode == 0, result.stderr
        pg.passwords["BANK_STUB_DB_PASSWORD"] = rotated
        assert _run(pg, "bank_stub_app", "select current_user") == "bank_stub_app"
        pg.passwords["BANK_STUB_DB_PASSWORD"] = original
        with pytest.raises(asyncpg.InvalidPasswordError):
            _run(pg, "bank_stub_app", "select 1")
    finally:
        pg.passwords["BANK_STUB_DB_PASSWORD"] = original
        assert _psql(pg, "40-bank-stub.sql").returncode == 0
    assert _run(pg, "bank_stub_app", "select 1") == 1


def test_rerun_keeps_alembic_version_read_only(pg_bootstrapped: PgBootstrapped) -> None:
    """PAY-606, C63: after a re-run the runtime role can read the version table, never write it."""
    pg = pg_bootstrapped
    _run(
        pg,
        "payroll_migrator",
        "create table if not exists payroll.alembic_version(version_num varchar(32) primary key)",
    )
    _run(
        pg,
        "payroll_migrator",
        "revoke insert, update, delete on payroll.alembic_version from payroll_app",
    )
    result = _psql(pg, "20-payroll.sql")
    assert result.returncode == 0, result.stderr
    privileges = _run(
        pg,
        pg.admin_user,
        "select string_agg(privilege_type, ',' order by privilege_type) "
        "from information_schema.role_table_grants where table_schema='payroll' "
        "and table_name='alembic_version' and grantee='payroll_app'",
    )
    assert privileges == "SELECT"

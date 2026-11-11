"""The shared pycommon.testing container fixtures (platform LLD §3.14; PAY-606, PAY-607).

They mount the lead-owned ``deploy/compose`` assets; skipped when those aren't present.
"""

import asyncio

import asyncpg  # type: ignore[import-untyped]
import pytest
import redis
import redis.exceptions
from pycommon.testing import (
    DB_ROLE_PASSWORD_ENV,
    REDIS_USER_PASSWORD_ENV,
    PgBootstrapped,
    RedisAcl,
    find_repo_root,
)

pytestmark = pytest.mark.integration


def _assets(sub: str) -> bool:
    try:
        return (find_repo_root() / "deploy" / "compose" / sub).is_dir()
    except FileNotFoundError:
        return False


@pytest.mark.skipif(not _assets("init"), reason="deploy/compose/init not present")
def test_pg_bootstrapped_roles_and_env(pg_bootstrapped: PgBootstrapped) -> None:
    """PAY-606: the bootstrap SQL runs; db_env_for gives the C1 keys; runtime roles are isolated."""
    env = pg_bootstrapped.env_for("payroll_app")
    assert set(env) == {"DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SCHEMA"}
    assert (env["DB_USER"], env["DB_SCHEMA"], env["DB_NAME"]) == (
        "payroll_app",
        "payroll",
        "payrun",
    )
    assert pg_bootstrapped.url_for("payroll_app").startswith("postgresql+asyncpg://payroll_app:")
    assert pg_bootstrapped.env_for("postgres")["DB_SCHEMA"] == "public"
    with pytest.raises(KeyError):
        pg_bootstrapped.env_for("nobody")
    assert set(pg_bootstrapped.passwords) == set(DB_ROLE_PASSWORD_ENV.values())

    async def check() -> None:
        conn = await asyncpg.connect(
            host=env["DB_HOST"], port=int(env["DB_PORT"]), user="payroll_app",
            password=env["DB_PASSWORD"], database="payrun",
        )  # fmt: skip
        try:
            assert await conn.fetchval("SHOW search_path") == "payroll"
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("CREATE TABLE identity.x (id int)")
        finally:
            await conn.close()

    asyncio.run(check())


@pytest.mark.skipif(not _assets("redis"), reason="deploy/compose/redis not present")
def test_redis_acl_users(redis_acl: RedisAcl) -> None:
    """PAY-607: every ACL user can PING; web can't XADD to the payout stream."""
    for user in REDIS_USER_PASSWORD_ENV:
        env = redis_acl.env_for(user)
        assert set(env) == {"REDIS_HOST", "REDIS_PORT", "REDIS_USERNAME", "REDIS_PASSWORD"}
        client = redis.Redis(
            host=env["REDIS_HOST"], port=int(env["REDIS_PORT"]),
            username=env["REDIS_USERNAME"], password=env["REDIS_PASSWORD"],
        )  # fmt: skip
        try:
            assert client.ping()
            if user == "web":
                with pytest.raises(redis.exceptions.NoPermissionError):
                    client.xadd("stream:payout.requested", {"a": "b"})
        finally:
            client.close()

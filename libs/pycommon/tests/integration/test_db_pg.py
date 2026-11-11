"""DB timeouts, outages and migrations on real Postgres (platform LLD §3.7, §6.1; PAY-605, 606)."""

import asyncio
import io
import time
from pathlib import Path
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from alembic import command
from alembic.config import Config
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from pycommon.app import create_app, get_ctx
from pycommon.db import create_engine, map_db_errors, session_factory, transaction
from pycommon.health import MigrationsMismatchError, migrations_check
from pycommon.testing import JsonLogCapture
from sqlalchemy import text

from tests.integration.conftest import Pg

pytestmark = pytest.mark.integration


def _router() -> APIRouter:
    r = APIRouter()

    @r.get("/sleep/{seconds}")
    async def sleep(seconds: float, request: Request) -> dict[str, str]:
        sf = get_ctx(request).sessions
        assert sf is not None
        async with transaction(sf) as s:
            await s.execute(text("SELECT pg_sleep(:s)"), {"s": seconds})
        return {"ok": "yes"}

    @r.get("/one")
    async def one(request: Request) -> dict[str, int]:
        engine = get_ctx(request).engine
        assert engine is not None
        with map_db_errors("one"):
            async with engine.connect() as conn:
                return {"v": (await conn.execute(text("SELECT 1"))).scalar_one()}

    return r


def _client(pg: Pg, **settings: Any) -> TestClient:
    app = create_app(
        pg.settings(**settings),
        public_routers=[_router()],
        use_redis=False,
        use_jwt=False,
        instrument=False,
    )
    return TestClient(app, raise_server_exceptions=False)


def test_statement_timeout_is_503_db_timeout(pg: Pg, caplog_json: JsonLogCapture) -> None:
    """PAY-605: a query past DB_STATEMENT_TIMEOUT_MS (2 s) → 503 db_timeout."""
    with _client(pg) as c:
        start = time.monotonic()
        resp = c.get("/sleep/3")
        elapsed = time.monotonic() - start
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "db_timeout"
        assert 1.8 < elapsed < 2.9
        assert c.get("/sleep/0").status_code == 200
    assert caplog_json.events("db.timeout")


def test_pool_exhaustion_is_db_timeout_after_pool_timeout(pg: Pg) -> None:
    """PAY-605: no free pooled connection within DB_POOL_TIMEOUT_S (2 s) → 503 db_timeout."""

    async def scenario() -> tuple[int, float]:
        settings = pg.settings(db_pool_size=1, db_max_overflow=0)
        engine = create_engine(settings)
        try:
            async with engine.connect() as held:
                await held.execute(text("SELECT 1"))
                start = time.monotonic()
                try:
                    with map_db_errors("pool"):
                        async with engine.connect() as conn:
                            await conn.execute(text("SELECT 1"))
                except Exception as exc:
                    return getattr(exc, "status", 0), time.monotonic() - start
                return 200, 0.0
        finally:
            await engine.dispose()

    status, elapsed = asyncio.run(scenario())
    assert status == 503
    assert 1.8 < elapsed < 3.0


def test_db_down_is_fast_db_unavailable_and_recovers(restartable_pg: Pg) -> None:
    """PAY-605: DB stopped → 503 db_unavailable fast; after restart no stuck pool connections."""
    with _client(restartable_pg) as c:
        assert c.get("/one").json() == {"v": 1}
        restartable_pg.stop()
        try:
            start = time.monotonic()
            resp = c.get("/one")
            assert resp.status_code == 503
            assert resp.json()["error"]["code"] == "db_unavailable"
            assert time.monotonic() - start < 3.0
            assert c.get("/readyz").status_code == 503
        finally:
            restartable_pg.start()
        for _ in range(3):
            assert c.get("/one").json() == {"v": 1}
        assert c.get("/readyz").json()["checks"] == {"db": "ok"}


def _alembic_project(tmp_path: Path, revision: str = "a1") -> Config:
    root = tmp_path / "svc"
    versions = root / "migrations" / "versions"
    versions.mkdir(parents=True)
    (root / "migrations" / "script.py.mako").write_text("")
    (root / "migrations" / "env.py").write_text(
        "from sqlalchemy import MetaData\n"
        "from pycommon.db import run_alembic_env\n"
        "run_alembic_env(MetaData(schema='pyc'), schema='pyc', runtime_role='pyc_app')\n"
    )
    (versions / f"{revision}_widgets.py").write_text(
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        f"revision = {revision!r}\n"
        "down_revision = None\n"
        "def upgrade() -> None:\n"
        "    op.create_table('widgets', sa.Column('id', sa.Integer, primary_key=True),\n"
        "                    schema='pyc')\n"
        "def downgrade() -> None:\n"
        "    op.drop_table('widgets', schema='pyc')\n"
    )
    ini = root / "alembic.ini"
    ini.write_text(f"[alembic]\nscript_location = {root / 'migrations'}\n")
    return Config(str(ini))


@pytest.fixture
def migrator_env(pg: Pg, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for k, v in pg.env("pyc_migrator").items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)


async def _q(pg: Pg, user: str, sql: str) -> Any:
    conn = await asyncpg.connect(**pg.dsn(user))
    try:
        return await conn.fetch(sql)
    finally:
        await conn.close()


def test_run_alembic_env_version_table_and_revoke(
    pg: Pg, migrator_env: None, tmp_path: Path
) -> None:
    """PAY-606: the version table is in the schema; the runtime role reads but cannot write it."""
    cfg = _alembic_project(tmp_path)
    command.upgrade(cfg, "head")
    rows = asyncio.run(_q(pg, "pyc_app", "SELECT version_num FROM pyc.alembic_version"))
    assert [r["version_num"] for r in rows] == ["a1"]
    tables = asyncio.run(
        _q(pg, "postgres", "SELECT schemaname FROM pg_tables WHERE tablename = 'alembic_version'")
    )
    assert [r["schemaname"] for r in tables] == ["pyc"]
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        asyncio.run(_q(pg, "pyc_app", "INSERT INTO pyc.alembic_version VALUES ('x')"))
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        asyncio.run(_q(pg, "pyc_app", "DELETE FROM pyc.alembic_version"))
    assert asyncio.run(_q(pg, "pyc_app", "SELECT count(*) AS n FROM pyc.widgets"))[0]["n"] == 0

    buf = io.StringIO()
    cfg.output_buffer = buf
    command.upgrade(cfg, "head", sql=True)  # offline mode renders SQL without connecting
    assert "CREATE TABLE pyc.widgets" in buf.getvalue()


async def test_migrations_check_detects_head_mismatch(
    pg: Pg, migrator_env: None, tmp_path: Path
) -> None:
    """PAY-605: /readyz migrations check fails when the DB is not at the scripts' head."""
    cfg = _alembic_project(tmp_path)
    command.downgrade(cfg, "base")  # called inside a running loop: env runs on a worker thread
    command.upgrade(cfg, "head")
    engine = create_engine(pg.settings())
    try:
        location = str(tmp_path / "svc" / "migrations")
        check = migrations_check(engine, schema="pyc", script_location=location)
        await check()
        other = tmp_path / "other"
        _alembic_project(other, revision="b2")
        stale = migrations_check(
            engine, schema="pyc", script_location=str(other / "svc" / "migrations")
        )
        with pytest.raises(MigrationsMismatchError):
            await stale()
        sf = session_factory(engine)
        async with transaction(sf) as s:
            assert (await s.execute(text("SHOW search_path"))).scalar_one() == "pyc"
    finally:
        await engine.dispose()

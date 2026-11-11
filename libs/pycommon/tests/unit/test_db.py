"""DB engine arguments, error mapping, IDs and lock keys (platform LLD §3.7; PAY-605, PAY-608)."""

import asyncio
import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
import sqlalchemy.exc
from pycommon import db
from pycommon.db import (
    Base,
    TimestampMixin,
    advisory_lock_key,
    create_engine,
    map_db_errors,
    new_id,
    session_factory,
)
from pycommon.errors import DbTimeout, DbUnavailable
from pycommon.testing import JsonLogCapture
from pydantic import SecretStr
from sqlalchemy import Column, Integer, MetaData, Table

from tests.helpers import make_settings


def test_create_engine_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """PAY-605: statement timeout, search path, pool timeout, pre-ping, recycle, connect timeout."""
    captured: dict[str, Any] = {}

    def fake_create(url: str, **kwargs: Any) -> str:
        captured["url"] = url
        captured.update(kwargs)
        return "engine"

    monkeypatch.setattr(db, "create_async_engine", fake_create)
    settings = make_settings(
        db_host="pg", db_statement_timeout_ms=2000, db_pool_timeout_s=2.0, db_schema="payroll"
    )
    assert create_engine(settings) == "engine"
    assert captured["url"].startswith("postgresql+asyncpg://testsvc_app:")
    assert captured["pool_size"] == 10
    assert captured["max_overflow"] == 5
    assert captured["pool_timeout"] == 2.0
    assert captured["pool_pre_ping"] is True
    assert captured["pool_recycle"] == 1800
    assert captured["connect_args"] == {
        "server_settings": {
            "statement_timeout": "2000",
            "lock_timeout": "1000",
            "idle_in_transaction_session_timeout": "10000",
            "application_name": "testsvc",
            "search_path": "payroll",
        },
        "timeout": 2,
        "command_timeout": 2.5,
    }
    assert db.default_db_budget_s() == 4.0
    create_engine(settings, user="payroll_migrator", password=SecretStr("m-pass"))
    assert captured["url"].startswith("postgresql+asyncpg://payroll_migrator:m-pass@pg:5432/")


async def test_real_engine_pool_settings() -> None:
    """PAY-605: the real AsyncEngine carries the pool settings (no connection is opened)."""
    engine = create_engine(make_settings(db_pool_size=3, db_pool_timeout_s=1.5))
    try:
        pool: Any = engine.pool
        assert pool.size() == 3
        assert pool._timeout == 1.5
        assert pool._pre_ping is True
        assert pool._recycle == 1800
        sf = session_factory(engine)
        assert sf.kw["expire_on_commit"] is False
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("exc", "mapped", "event"),
    [
        (asyncpg.QueryCanceledError("statement timeout"), DbTimeout, "db.timeout"),
        (sqlalchemy.exc.TimeoutError("pool"), DbTimeout, "db.timeout"),
        (TimeoutError(), DbTimeout, "db.timeout"),
        (asyncpg.ConnectionDoesNotExistError("closed"), DbUnavailable, "db.unavailable"),
        (
            sqlalchemy.exc.OperationalError("SELECT 1", {}, OSError("refused")),
            DbUnavailable,
            "db.unavailable",
        ),
        (ConnectionRefusedError(61, "refused"), DbUnavailable, "db.unavailable"),
        (
            sqlalchemy.exc.InterfaceError("SELECT", {}, Exception("closed")),
            DbUnavailable,
            "db.unavailable",
        ),
    ],
)
def test_map_db_errors(
    caplog_json: JsonLogCapture, exc: Exception, mapped: type[Exception], event: str
) -> None:
    """PAY-605: each driver error maps to DbTimeout or DbUnavailable (503) and logs {operation}."""
    with pytest.raises(mapped) as info, map_db_errors("payroll.load"):
        raise exc
    assert info.value.__cause__ is exc
    (logged,) = caplog_json.events(event)
    assert logged["operation"] == "payroll.load"
    assert isinstance(info.value, DbTimeout | DbUnavailable)
    assert info.value.status == 503


def test_map_db_errors_passes_other_errors_through() -> None:
    """PAY-605: integrity errors and the like are not mapped."""
    err = sqlalchemy.exc.IntegrityError("INSERT", {}, Exception("dup"))
    with pytest.raises(sqlalchemy.exc.IntegrityError), map_db_errors():
        raise err
    with pytest.raises(DbUnavailable), map_db_errors():
        raise DbUnavailable()


def test_advisory_lock_key_is_stable_and_signed_64_bit() -> None:
    """PAY-608: the lock key is the signed 64-bit prefix of sha256(name)."""
    key = advisory_lock_key("payroll:maintenance")
    assert key == advisory_lock_key("payroll:maintenance")
    assert key != advisory_lock_key("identity:maintenance")
    assert -(2**63) <= key < 2**63
    keys = [advisory_lock_key(f"svc{i}:maintenance") for i in range(64)]
    assert any(k < 0 for k in keys)
    assert advisory_lock_key("a") == int.from_bytes(
        bytes.fromhex("ca978112ca1bbdca"), "big", signed=True
    )


def test_new_id_is_uuid7_and_time_ordered() -> None:
    """PAY-402: new_id gives UUIDv7s that sort in creation order."""
    ids = [new_id() for _ in range(200)]
    assert all(isinstance(i, uuid.UUID) and i.version == 7 for i in ids)
    assert ids == sorted(ids)
    assert len(set(ids)) == 200


def test_timestamp_mixin_columns() -> None:
    """PAY-608: created_at/updated_at are timestamptz with server defaults; updated_at onupdate."""

    class Thing(TimestampMixin, Base):
        __tablename__ = "thing"
        id: Any = Column(Integer, primary_key=True)

    table: Table = Thing.__table__  # type: ignore[assignment]
    created, updated = table.c.created_at, table.c.updated_at
    assert created.type.timezone is True  # type: ignore[attr-defined]
    assert created.server_default is not None
    assert not created.nullable
    assert updated.onupdate is not None
    assert isinstance(Base.metadata, MetaData)


def test_identifier_checks() -> None:
    """PAY-606: run_alembic_env only accepts plain identifiers for schema and role."""
    assert db._check_ident("payroll", "schema") == "payroll"
    with pytest.raises(ValueError, match="schema"):
        db._check_ident("Pay roll", "schema")


# --- the DB time budget (S8 slow-DB finding; PAY-605, HLD §4 "Slow DB") ----------------------


class _FakeTx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    def begin(self) -> _FakeTx:
        return _FakeTx()


def test_db_budget_is_statement_plus_pool_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget follows DB_STATEMENT_TIMEOUT_MS / 1000 + DB_POOL_TIMEOUT_S."""
    monkeypatch.setattr(db, "create_async_engine", lambda url, **kw: "engine")
    monkeypatch.setitem(db._budget, "s", 4.0)
    s = make_settings(db_statement_timeout_ms=1500, db_pool_timeout_s=1.0)
    assert db.db_budget_s(s) == 2.5
    create_engine(s)
    assert db.default_db_budget_s() == 2.5


async def test_map_db_errors_budget_is_db_timeout(caplog_json: JsonLogCapture) -> None:
    """A unit of DB work that outlives its budget ends with 503 db_timeout, on time."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(DbTimeout), map_db_errors("slow", budget_s=0.05):
        await asyncio.sleep(5)
    assert loop.time() - start < 1
    (logged,) = caplog_json.events("db.timeout")
    assert logged["operation"] == "slow"
    task = asyncio.current_task()
    assert task is not None
    assert task.cancelling() == 0  # the deadline's own cancellation was taken back
    await asyncio.sleep(0)  # and the task keeps running normally


async def test_map_db_errors_budget_when_driver_rewraps_the_cancellation() -> None:
    """If the driver turns the deadline's cancellation into its own error, it's still a timeout."""

    async def driver() -> None:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError as exc:
            raise sqlalchemy.exc.InterfaceError("SELECT", {}, Exception("cancelled")) from exc

    with pytest.raises(DbTimeout), map_db_errors(budget_s=0.05):
        await driver()


async def test_map_db_errors_within_budget_is_untouched() -> None:
    with map_db_errors(budget_s=1):
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.06)  # the timer was cancelled: nothing fires later


async def test_map_db_errors_outside_cancellation_propagates() -> None:
    """A cancellation that isn't the budget's (shutdown, client gone) stays a CancelledError."""

    async def work() -> None:
        with map_db_errors(budget_s=5):
            await asyncio.sleep(5)

    t = asyncio.create_task(work())
    await asyncio.sleep(0.01)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t


def test_map_db_errors_without_a_loop_has_no_budget() -> None:
    """Outside a coroutine (sync callers) there's nothing to cancel, so no deadline is armed."""
    with map_db_errors(budget_s=0.0):
        pass


async def test_transaction_budget_is_db_timeout(caplog_json: JsonLogCapture) -> None:
    session = _FakeSession()
    with pytest.raises(DbTimeout):
        async with db.transaction(lambda: session, budget_s=0.05):  # type: ignore[arg-type]
            await asyncio.sleep(5)
    assert session.closed
    (logged,) = caplog_json.events("db.timeout")
    assert logged["operation"] == "transaction"


async def test_transaction_uses_the_default_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(db._budget, "s", 0.05)
    with pytest.raises(DbTimeout):
        async with db.transaction(_FakeSession):  # type: ignore[arg-type]
            await asyncio.sleep(5)
    async with db.transaction(_FakeSession) as s:  # type: ignore[arg-type]
        assert s is not None


async def test_map_db_errors_budget_also_cuts_slow_cleanup() -> None:
    """Cleanup inside the block (a rollback over the same slow link) is cancelled too, so the
    whole block ends close to the budget, not budget + cleanup."""
    loop = asyncio.get_running_loop()
    start = loop.time()

    async def body() -> None:
        try:
            await asyncio.sleep(5)
        finally:
            await asyncio.sleep(5)  # stands in for the rollback and connection release

    with pytest.raises(DbTimeout), map_db_errors(budget_s=0.05):
        await body()
    assert loop.time() - start < 1
    task = asyncio.current_task()
    assert task is not None
    assert task.cancelling() == 0

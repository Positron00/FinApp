"""Async engine, sessions, error mapping, IDs and the Alembic env helper (platform LLD §3.7).

Timeouts (503 ``db_timeout``) come from three layers:

- a slow *query* is cancelled by Postgres at ``DB_STATEMENT_TIMEOUT_MS`` (``QueryCanceledError``);
- a slow *link* (network latency, a stalled server) is caught by asyncpg's per-command
  ``command_timeout`` (statement timeout + 0.5 s), which the server-side timeout can't see;
- the whole unit of DB work in :func:`transaction` / :func:`map_db_errors` has a budget of
  ``DB_STATEMENT_TIMEOUT_MS / 1000 + DB_POOL_TIMEOUT_S``, so many slow round trips (pre-ping,
  prepare, execute, commit) can't add up past it (S8 slow-DB finding).

A dead DB yields a fast connect failure (503 ``db_unavailable``); ``pool_pre_ping`` drops broken
connections.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import re
import uuid
from collections.abc import AsyncIterator, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from typing import Any, Final
from urllib.parse import quote
from uuid import UUID

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import DateTime, MetaData, func, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pycommon.errors import (
    INVALID_TEXT_DETAILS,
    DbTimeout,
    DbUnavailable,
    ValidationFailed,
    db_error_kind,
    is_invalid_text,
)
from pycommon.logging import get_logger, log_event
from pycommon.settings import BaseServiceSettings

__all__ = [
    "Base",
    "TimestampMixin",
    "advisory_lock_key",
    "create_engine",
    "db_budget_s",
    "default_db_budget_s",
    "map_db_errors",
    "new_id",
    "run_alembic_env",
    "session_factory",
    "transaction",
]

_log = get_logger("pycommon.db")
_IDENT: Final = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
POOL_RECYCLE_S: Final = 1800
LOCK_TIMEOUT_MS: Final = 1000
IDLE_IN_TRANSACTION_TIMEOUT_MS: Final = 10000
CONNECT_TIMEOUT_S: Final = 2
COMMAND_TIMEOUT_MARGIN_S: Final = 0.5  # lets Postgres' own statement_timeout answer first

# The default budget for transaction() and map_db_errors(): the code defaults (2 s statement
# timeout + 2 s pool timeout) until create_engine() records the service's settings.
_budget: dict[str, float] = {"s": 4.0}


def db_budget_s(settings: BaseServiceSettings) -> float:
    """``DB_STATEMENT_TIMEOUT_MS / 1000 + DB_POOL_TIMEOUT_S``: one unit of DB work's time budget."""
    return settings.db_statement_timeout_ms / 1000 + settings.db_pool_timeout_s


def default_db_budget_s() -> float:
    return _budget["s"]


def create_engine(
    settings: BaseServiceSettings, *, user: str | None = None, password: SecretStr | None = None
) -> AsyncEngine:
    _budget["s"] = db_budget_s(settings)
    return create_async_engine(
        settings.db_url(user=user, password=password),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        pool_pre_ping=True,
        pool_recycle=POOL_RECYCLE_S,
        connect_args={
            "server_settings": {
                "statement_timeout": str(settings.db_statement_timeout_ms),
                "lock_timeout": str(LOCK_TIMEOUT_MS),
                "idle_in_transaction_session_timeout": str(IDLE_IN_TRANSACTION_TIMEOUT_MS),
                "application_name": settings.service_name,
                "search_path": settings.db_schema,
            },
            "timeout": CONNECT_TIMEOUT_S,
            # Per-command client-side bound: latency on the link isn't seen by statement_timeout.
            "command_timeout": settings.db_statement_timeout_ms / 1000 + COMMAND_TIMEOUT_MARGIN_S,
        },
    )


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def transaction(
    sf: async_sessionmaker[AsyncSession], *, budget_s: float | None = None
) -> AsyncIterator[AsyncSession]:
    """One session in one transaction: committed on success, rolled back on error.

    The block, commit and cleanup included, must finish within ``budget_s`` (default: the
    service's ``DB_STATEMENT_TIMEOUT_MS / 1000 + DB_POOL_TIMEOUT_S``), else 503 ``db_timeout``;
    driver errors are mapped as in :func:`map_db_errors`, which provides the deadline. That
    deadline behaves like ``asyncio.timeout`` but keeps cancelling until the block has exited,
    so the rollback and connection release can't wait on a slow link a second time
    (``asyncio.timeout`` cancels once, and its cleanup then took ~4 s more at 2 s latency).
    """
    with map_db_errors("transaction", budget_s=budget_s):
        async with sf() as session, session.begin():
            yield session


class _Deadline:
    """``asyncio.timeout`` for a *synchronous* ``with`` block inside a coroutine.

    ``map_db_errors`` is a plain context manager (callers write ``with map_db_errors(): async
    with …``), so it can't ``async with asyncio.timeout``. This does what ``asyncio.Timeout``
    does: cancel the current task at the deadline, and on exit take its cancellations back
    (``Task.uncancel``); a cancellation from anywhere else still propagates.

    Unlike ``asyncio.Timeout`` it keeps cancelling every ``RECANCEL_S`` until the block exits:
    the block's own cleanup (rollback, returning the connection to the pool) would otherwise
    wait on the same slow link. SQLAlchemy terminates a connection whose release is cancelled.
    """

    RECANCEL_S: Final = 0.05

    def __init__(self, budget_s: float) -> None:
        self.expired = False
        self._closed = False
        self._own_cancel = False
        self._task = asyncio.current_task()
        self._handle: asyncio.TimerHandle | None = None
        self._cancelling = 0
        self._cancels = 0
        if self._task is not None:
            self._cancelling = self._task.cancelling()
            self._handle = asyncio.get_running_loop().call_later(budget_s, self._expire)

    def _expire(self) -> None:
        if self._closed or self._task is None or self._task.done():
            return
        self.expired = True
        self._cancels += 1
        self._task.cancel()
        self._handle = asyncio.get_running_loop().call_later(self.RECANCEL_S, self._expire)

    def close(self, exc: BaseException | None) -> bool:
        """Stop the timer. True when ``exc`` is this deadline's own cancellation. Idempotent."""
        if not self._closed:
            self._closed = True
            if self._handle is not None:
                self._handle.cancel()
            if self.expired and self._task is not None:
                left = self._task.cancelling()
                for _ in range(self._cancels):
                    left = self._task.uncancel()
                self._own_cancel = left <= self._cancelling and isinstance(
                    exc, asyncio.CancelledError
                )
        return self._own_cancel


def _in_coroutine() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return asyncio.current_task() is not None


@contextmanager
def map_db_errors(operation: str = "db", *, budget_s: float | None = None) -> Iterator[None]:
    """Map driver timeouts to ``DbTimeout`` and connection failures to ``DbUnavailable``.

    The block is a DB call, so a bare ``OSError``/``TimeoutError`` in it counts as a DB error.
    Text Postgres rejects (SQLSTATE 22021, e.g. a NUL byte) becomes 422 ``validation_error`` (C63).
    Inside a coroutine the block also has a time budget (``budget_s``, default the service's
    ``DB_STATEMENT_TIMEOUT_MS / 1000 + DB_POOL_TIMEOUT_S``): past it, 503 ``db_timeout``.
    """
    budget = default_db_budget_s() if budget_s is None else budget_s
    deadline = _Deadline(budget) if _in_coroutine() else None
    try:
        yield
    except BaseException as exc:
        own_cancel = deadline is not None and deadline.close(exc)
        if own_cancel or (
            deadline is not None
            and deadline.expired
            and not isinstance(exc, asyncio.CancelledError)
        ):
            # The budget ran out (the driver may have turned the cancellation into its own error).
            log_event(_log, logging.WARNING, "db.timeout", operation=operation, budget_s=budget)
            raise DbTimeout() from exc
        if not isinstance(exc, Exception) or isinstance(
            exc, DbTimeout | DbUnavailable | ValidationFailed
        ):
            raise
        if is_invalid_text(exc):
            raise ValidationFailed(details=[dict(d) for d in INVALID_TEXT_DETAILS]) from exc
        kind = db_error_kind(exc, in_db_call=True)
        if kind == "timeout":
            log_event(_log, logging.WARNING, "db.timeout", operation=operation)
            raise DbTimeout() from exc
        if kind == "unavailable":
            log_event(_log, logging.WARNING, "db.unavailable", operation=operation)
            raise DbUnavailable() from exc
        raise
    finally:
        if deadline is not None:
            deadline.close(None)


def new_id() -> UUID:
    """A UUIDv7 (time-ordered; Python 3.14 stdlib)."""
    return uuid.uuid7()


def advisory_lock_key(name: str) -> int:
    """A signed 64-bit Postgres advisory-lock key from ``sha256(name)[:8]``."""
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class _MigrateSettings(BaseSettings):
    """Only the C1 DB keys: migrate containers set them with migrator credentials."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "payrun"
    db_user: str
    db_password: SecretStr


def _check_ident(value: str, what: str) -> str:
    if not _IDENT.match(value):
        raise ValueError(f"{what} must be a plain lower-case identifier")
    return value


def _run_coro(coro: Coroutine[Any, Any, None]) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return
    # Called from inside a running loop (e.g. a test): run on a fresh loop in a worker thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, coro).result()


def run_alembic_env(target_metadata: MetaData, *, schema: str, runtime_role: str) -> None:
    """The body of each service's ``migrations/env.py`` (PAY-606).

    The version table lives in ``schema``; only that schema is compared; after ``upgrade`` the
    runtime role loses ``INSERT, UPDATE, DELETE`` on ``<schema>.alembic_version``.
    """
    from alembic import context  # noqa: PLC0415  (only importable inside an Alembic run)

    _check_ident(schema, "schema")
    _check_ident(runtime_role, "runtime_role")
    s = _MigrateSettings()
    url = (
        f"postgresql+asyncpg://{quote(s.db_user, safe='')}:"
        f"{quote(s.db_password.get_secret_value(), safe='')}@{s.db_host}:{s.db_port}/{s.db_name}"
    )

    def include_name(name: str | None, type_: str, parent_names: Any) -> bool:
        if type_ == "schema":
            return name == schema
        return True

    options: dict[str, Any] = {
        "target_metadata": target_metadata,
        "version_table_schema": schema,
        "include_schemas": True,
        "include_name": include_name,
    }

    if context.is_offline_mode():
        context.configure(url=url, literal_binds=True, **options)
        with context.begin_transaction():
            context.run_migrations()
        return

    def do_run(connection: Connection) -> None:
        context.configure(connection=connection, **options)
        with context.begin_transaction():
            context.run_migrations()
        if connection.execute(
            text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"{schema}.alembic_version"}
        ).scalar_one():
            connection.execute(
                text(
                    f'REVOKE INSERT, UPDATE, DELETE ON "{schema}".alembic_version '
                    f'FROM "{runtime_role}"'
                )
            )
        connection.commit()

    async def run_online() -> None:
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        try:
            async with engine.connect() as conn:
                await conn.run_sync(do_run)
        finally:
            await engine.dispose()

    _run_coro(run_online())

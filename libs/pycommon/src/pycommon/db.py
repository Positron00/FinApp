"""Async engine, sessions, error mapping, IDs and the Alembic env helper (platform LLD §3.7).

A slow DB yields ``QueryCanceledError`` at ``DB_STATEMENT_TIMEOUT_MS`` (503 ``db_timeout``); a
dead DB yields a fast connect failure (503 ``db_unavailable``); ``pool_pre_ping`` drops broken
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


def create_engine(
    settings: BaseServiceSettings, *, user: str | None = None, password: SecretStr | None = None
) -> AsyncEngine:
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
        },
    )


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def transaction(sf: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """One session in one transaction: committed on success, rolled back on error."""
    async with sf() as session, session.begin():
        yield session


@contextmanager
def map_db_errors(operation: str = "db") -> Iterator[None]:
    """Map driver timeouts to ``DbTimeout`` and connection failures to ``DbUnavailable``.

    The block is a DB call, so a bare ``OSError``/``TimeoutError`` in it counts as a DB error.
    Text Postgres rejects (SQLSTATE 22021, e.g. a NUL byte) becomes 422 ``validation_error`` (C63).
    """
    try:
        yield
    except DbTimeout, DbUnavailable, ValidationFailed:
        raise
    except Exception as exc:
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

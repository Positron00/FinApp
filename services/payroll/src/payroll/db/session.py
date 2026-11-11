"""Sessions (design §1): the async engine and sessionmaker come from pycommon's ``create_app``.

``session_scope`` is the one transaction helper the services layer uses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pycommon.db import map_db_errors
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["session_scope"]


@asynccontextmanager
async def session_scope(
    sessions: async_sessionmaker[AsyncSession], operation: str = "db"
) -> AsyncIterator[AsyncSession]:
    """One session in one READ COMMITTED transaction: committed on success, rolled back on error."""
    with map_db_errors(operation):
        async with sessions() as session, session.begin():
            yield session

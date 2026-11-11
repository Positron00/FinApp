"""Identity maintenance (identity LLD §6.10; PAY-608, PAY-NFR-08, C44, C50, C63).

pycommon's ``MaintenanceRunner`` runs these under the advisory lock ``identity:maintenance``,
each task in its own transaction. Cutoffs come from the ``Clock`` so tests can move time;
the audit purge goes through ``identity.purge_audit_log``, which clamps any cutoff to at least
400 days old (the runtime role has no ``DELETE`` on ``audit_log``).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Final

from pycommon import Clock, SystemClock
from pycommon.maintenance import MaintenanceRunner, MaintenanceTask
from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from identity.settings import IdentitySettings

__all__ = ["BATCH", "LOCK_NAME", "TASK_NAMES", "build_runner", "identity_tasks"]

LOCK_NAME: Final = "identity:maintenance"
BATCH: Final = 1000
TASK_NAMES: Final = (
    "null_invite_hashes",
    "purge_invites",
    "purge_password_resets",
    "purge_emails",
    "purge_audit",
)

# Each statement touches at most BATCH rows; the task repeats it until a short batch.
_NULL_INVITE_HASHES: Final = text(
    """
    UPDATE identity.invites SET token_hash = NULL, updated_at = now()
     WHERE id IN (SELECT id FROM identity.invites
                   WHERE token_hash IS NOT NULL AND expires_at < :cutoff
                   LIMIT :batch)
    """
)
# A never-sent invite has expires_at NULL, so its accept or revoke time (updated_at) counts.
_PURGE_INVITES: Final = text(
    """
    DELETE FROM identity.invites
     WHERE id IN (SELECT id FROM identity.invites
                   WHERE status IN ('accepted', 'revoked')
                     AND coalesce(expires_at, updated_at) < :cutoff
                   LIMIT :batch)
    """
)
_PURGE_RESETS: Final = text(
    """
    DELETE FROM identity.password_resets
     WHERE id IN (SELECT id FROM identity.password_resets
                   WHERE coalesce(expires_at, updated_at) < :cutoff
                   LIMIT :batch)
    """
)
_PURGE_EMAILS: Final = text(
    """
    DELETE FROM identity.email_outbox
     WHERE id IN (SELECT id FROM identity.email_outbox
                   WHERE status IN ('sent', 'failed', 'cancelled') AND finished_at < :cutoff
                   LIMIT :batch)
    """
)


async def _in_batches(session: AsyncSession, stmt: TextClause, cutoff: datetime) -> int:
    total = 0
    while True:
        result = await session.execute(stmt, {"cutoff": cutoff, "batch": BATCH})
        n = int(getattr(result, "rowcount", 0) or 0)
        total += n
        if n < BATCH:
            return total


def _batched(name: str, stmt: TextClause, cutoff: Callable[[], datetime]) -> MaintenanceTask:
    async def run(session: AsyncSession) -> int:
        return await _in_batches(session, stmt, cutoff())

    return MaintenanceTask(name, run)


def identity_tasks(settings: IdentitySettings, clock: Clock | None = None) -> list[MaintenanceTask]:
    """The five §6.10 tasks, in order (names as ``TASK_NAMES``)."""
    c = clock or SystemClock()
    s = settings

    def tokens_cutoff() -> datetime:
        return c.now() - timedelta(days=s.retention_tokens_days)

    def outbox_cutoff() -> datetime:
        return c.now() - timedelta(days=s.retention_outbox_days)

    async def purge_audit(session: AsyncSession) -> int:
        cutoff = c.now() - timedelta(days=s.identity_retention_audit_days)
        result = await session.execute(
            text("SELECT identity.purge_audit_log(:cutoff)"), {"cutoff": cutoff}
        )
        return int(result.scalar_one())

    return [
        _batched("null_invite_hashes", _NULL_INVITE_HASHES, tokens_cutoff),
        _batched("purge_invites", _PURGE_INVITES, tokens_cutoff),
        _batched("purge_password_resets", _PURGE_RESETS, tokens_cutoff),
        _batched("purge_emails", _PURGE_EMAILS, outbox_cutoff),
        MaintenanceTask("purge_audit", purge_audit),
    ]


def build_runner(
    settings: IdentitySettings,
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock | None = None,
) -> MaintenanceRunner:
    return MaintenanceRunner(
        engine,
        sessions,
        lock_name=LOCK_NAME,
        tasks=identity_tasks(settings, clock),
        interval_s=settings.maintenance_interval_s,
        clock=clock,
    )

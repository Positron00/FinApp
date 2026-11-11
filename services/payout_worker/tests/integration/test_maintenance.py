"""Maintenance: the processed_events purge (design §3.8; PAY-608, C44)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from payout_worker.maintenance import build_runner
from pycommon.db import create_engine, session_factory
from pycommon.testing import JsonLogCapture, PgBootstrapped, RedisAcl

from tests.integration.conftest import worker_settings

INSERT = (
    "INSERT INTO payouts.processed_events (event_id, stream, payout_id, attempt, company_id, "
    "outcome, processed_at) VALUES ($1, 'stream:payout.requested', $2, 1, $3, 'settled', "
    "now() - make_interval(days => $4))"
)


async def test_purge_old_processed_events(
    admin_db: Any, migrated: PgBootstrapped, redis_acl: RedisAcl, caplog_json: JsonLogCapture
) -> None:
    """PAY-608: rows older than RETENTION_PROCESSED_EVENTS_DAYS (30) go, newer stay; the task
    logs maintenance.task.completed with the count (C44); a held lock skips the tick."""
    for days in (31, 45, 29, 1):
        await admin_db.execute(INSERT, uuid4(), uuid4(), uuid4(), days)
    s = worker_settings(migrated, redis_acl)
    engine = create_engine(s)
    try:
        runner = build_runner(s, engine, session_factory(engine))
        assert await runner.tick()
        assert await admin_db.fetchval("SELECT count(*) FROM payouts.processed_events") == 2
        [event] = caplog_json.events("maintenance.task.completed")
        assert (event["task"], event["affected"]) == ("purge_processed_events", 2)
        other = await engine.connect()
        try:
            from pycommon.db import advisory_lock_key  # noqa: PLC0415
            from sqlalchemy import text  # noqa: PLC0415

            await other.execute(
                text("SELECT pg_advisory_lock(:k)"),
                {"k": advisory_lock_key("payout-worker:maintenance")},
            )
            await other.commit()
            assert not await runner.tick()
        finally:
            await other.close()
    finally:
        await engine.dispose()

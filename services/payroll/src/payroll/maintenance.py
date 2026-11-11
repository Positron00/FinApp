"""Payroll maintenance (design §5.3, §5.4; PAY-407, PAY-608, PAY-NFR-08, ADR 0004, C44).

pycommon's ``MaintenanceRunner`` runs the four tasks under the advisory lock
``payroll:maintenance``, in order, each in its own transaction (a failing task doesn't stop the
others): ``republish`` (§5.3), ``purge_outbox`` (sent rows older than ``RETENTION_OUTBOX_DAYS``),
``purge_idempotency_keys`` (past ``expires_at``) and ``purge_bank_details`` (§5.4.1). "now" and the
cutoffs come from ``Runtime.clock``, read at each tick, so tests move time with a fake clock.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from pycommon.logging import get_logger, log_event
from pycommon.maintenance import MaintenanceRunner, MaintenanceTask
from pycommon.otel import tracer
from sqlalchemy import TextClause, event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from payroll.db.repositories import AuditRepo, rowcount
from payroll.events.republish import SYSTEM_ACTOR, republish_due_attempts
from payroll.services.runtime import Runtime

__all__ = ["BANK_PURGE_BATCH", "BATCH", "LOCK_NAME", "TASK_NAMES", "build_maintenance", "tasks"]

LOCK_NAME: Final = "payroll:maintenance"
BATCH: Final = 1000
BANK_PURGE_BATCH: Final = 100
TASK_NAMES: Final = ("republish", "purge_outbox", "purge_idempotency_keys", "purge_bank_details")
_log: Final = get_logger("payroll.maintenance")

_PURGE_OUTBOX: Final = text(
    """
    DELETE FROM payroll.outbox
     WHERE id IN (SELECT id FROM payroll.outbox
                   WHERE sent_at IS NOT NULL AND sent_at < :cutoff
                   LIMIT :batch)
    """
)
_PURGE_IDEMPOTENCY: Final = text(
    """
    DELETE FROM payroll.idempotency_keys
     WHERE (company_id, scope, key) IN (SELECT company_id, scope, key
                                          FROM payroll.idempotency_keys
                                         WHERE expires_at < :cutoff
                                         LIMIT :batch)
    """
)
_NON_FINAL_EXISTS: Final = """
    EXISTS (SELECT 1 FROM payroll.payouts p JOIN payroll.payout_attempts a ON a.payout_id = p.id
             WHERE p.employee_id = e.id
               AND a.status IN ('requested', 'indeterminate', 'needs_operator'))
"""
_PURGE_CANDIDATES: Final = text(
    f"""
    SELECT e.id FROM payroll.employees e
     WHERE e.status = 'inactive' AND e.bank_account_enc IS NOT NULL
       AND e.deactivated_at < :cutoff
       AND NOT {_NON_FINAL_EXISTS}
     ORDER BY e.id
     LIMIT :batch
    """  # noqa: S608  (a constant fragment; every value is a bound parameter)
)
_LOCK_EMPLOYEE_ATTEMPTS: Final = text(
    """
    SELECT a.id FROM payroll.payout_attempts a JOIN payroll.payouts p ON p.id = a.payout_id
     WHERE p.employee_id = :id
     ORDER BY a.payout_id, a.attempt
       FOR UPDATE OF a
    """
)
_LOCK_EMPLOYEE: Final = text(
    """
    SELECT e.company_id, e.deactivated_at FROM payroll.employees e
     WHERE e.id = :id AND e.status = 'inactive' AND e.bank_account_enc IS NOT NULL
       AND e.deactivated_at < :cutoff
       FOR UPDATE
    """
)
# Run as its own statement once the employee lock is held, so READ COMMITTED gives it a fresh
# snapshot that sees an attempt committed by an approve that held the employee FOR SHARE.
_HAS_NON_FINAL: Final = text(
    f"SELECT {_NON_FINAL_EXISTS} FROM payroll.employees e WHERE e.id = :id"  # noqa: S608
)
_NULL_EMPLOYEE_BANK: Final = text(
    """
    UPDATE payroll.employees SET bank_routing_enc = NULL, bank_account_enc = NULL,
           account_last4 = NULL, bank_purged_at = :now, updated_at = now()
     WHERE id = :id
    """
)
_NULL_SNAPSHOTS: Final = text(
    """
    UPDATE payroll.payout_attempts a SET bank_snapshot_enc = NULL, updated_at = now()
      FROM payroll.payouts p
     WHERE a.payout_id = p.id AND p.employee_id = :id AND a.status IN ('settled', 'rejected')
       AND a.bank_snapshot_enc IS NOT NULL
    """
)


async def _in_batches(session: AsyncSession, stmt: TextClause, cutoff: datetime) -> int:
    total = 0
    while True:
        n = rowcount(await session.execute(stmt, {"cutoff": cutoff, "batch": BATCH}))
        total += n
        if n < BATCH:
            return total


async def purge_bank_details(session: AsyncSession, *, now: datetime, retention_days: int) -> int:
    """§5.4.1: null the bank details of employees deactivated more than ``retention_days`` ago
    with no non-final attempt, and their final attempts' snapshots. Locks each employee's attempt
    rows (order 5) before the employee row (order 6) and re-checks every condition under them."""
    cutoff = now - timedelta(days=retention_days)
    ids: list[UUID] = list(
        (
            await session.execute(_PURGE_CANDIDATES, {"cutoff": cutoff, "batch": BANK_PURGE_BATCH})
        ).scalars()
    )
    purged: list[tuple[UUID, int]] = []
    for employee_id in ids:
        await session.execute(_LOCK_EMPLOYEE_ATTEMPTS, {"id": employee_id})
        row = (await session.execute(_LOCK_EMPLOYEE, {"id": employee_id, "cutoff": cutoff})).first()
        if row is None or (await session.execute(_HAS_NON_FINAL, {"id": employee_id})).scalar():
            continue  # a re-check failed under the locks: skip this employee
        await session.execute(_NULL_EMPLOYEE_BANK, {"id": employee_id, "now": now})
        nulled = rowcount(await session.execute(_NULL_SNAPSHOTS, {"id": employee_id}))
        await AuditRepo.add(
            session, row.company_id, actor_id=SYSTEM_ACTOR, event="employee.bank_details_purged",
            entity_type="employee", entity_id=employee_id,
            details={"deactivated_at": row.deactivated_at.isoformat(), "attempts_nulled": nulled},
        )  # fmt: skip
        purged.append((employee_id, nulled))

    def report(_session: object) -> None:
        for employee_id, nulled in purged:
            log_event(
                _log, logging.INFO, "employee.bank_details_purged",
                employee_id=str(employee_id), attempts_nulled=nulled,
            )  # fmt: skip

    if purged:
        event.listen(session.sync_session, "after_commit", report, once=True)
    return len(purged)


def tasks(rt: Runtime) -> list[MaintenanceTask]:
    """The four §5.4 tasks, in order (names as ``TASK_NAMES``)."""
    s = rt.settings

    async def republish(session: AsyncSession) -> int:
        return await republish_due_attempts(
            session, settings=s, outbox=rt.outbox, metrics=rt.metrics, clock=rt.clock
        )

    async def purge_outbox(session: AsyncSession) -> int:
        cutoff = rt.clock.now() - timedelta(days=s.retention_outbox_days)
        with tracer("payroll").start_as_current_span("payroll.maintenance.purge") as span:
            n = await _in_batches(session, _PURGE_OUTBOX, cutoff)
            span.set_attribute("payroll.outbox_purged", n)
        return n

    async def purge_idempotency_keys(session: AsyncSession) -> int:
        with tracer("payroll").start_as_current_span("payroll.maintenance.purge") as span:
            n = await _in_batches(session, _PURGE_IDEMPOTENCY, rt.clock.now())
            span.set_attribute("payroll.idempotency_purged", n)
        return n

    async def bank_details(session: AsyncSession) -> int:
        with tracer("payroll").start_as_current_span("payroll.maintenance.purge") as span:
            n = await purge_bank_details(
                session, now=rt.clock.now(), retention_days=s.retention_bank_details_days
            )
            span.set_attribute("payroll.bank_purged", n)
        return n

    return [
        MaintenanceTask("republish", republish),
        MaintenanceTask("purge_outbox", purge_outbox),
        MaintenanceTask("purge_idempotency_keys", purge_idempotency_keys),
        MaintenanceTask("purge_bank_details", bank_details),
    ]


def build_maintenance(rt: Runtime, engine: AsyncEngine) -> MaintenanceRunner:
    """``MaintenanceRunner`` with lock ``payroll:maintenance`` every ``MAINTENANCE_INTERVAL_S``
    (60 s ± 10%)."""
    return MaintenanceRunner(
        engine,
        rt.sessions,
        lock_name=LOCK_NAME,
        tasks=tasks(rt),
        interval_s=rt.settings.maintenance_interval_s,
        jitter=0.1,
        clock=rt.clock,
    )

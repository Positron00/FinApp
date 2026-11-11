"""Recompute a run's status from its payouts after a payout change (PAY-406; design §4.7 step 5).

Shared by the result endpoint now, and by retry and resolve (F4a). ``processing_since`` is set on
each entry into ``processing``; ``first_final_at`` once, guarding the single observation of
``payroll_payout_settlement_seconds``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from pycommon.logging import get_logger, log_event
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from payroll.db.models import PayRun
from payroll.db.repositories import PayoutRepo
from payroll.domain.state import FINAL_RUN, PayoutStatus, RunStatus, run_status_from_payouts
from payroll.metrics import Metrics

__all__ = ["RunTransition", "recompute_run_status", "report_transition"]

_log: Final = get_logger("payroll.pay_runs")


@dataclass(frozen=True)
class RunTransition:
    run_id: UUID
    old: RunStatus
    new: RunStatus
    paid: int
    failed: int
    settlement_s: float | None  # set only on the run's first final status

    @property
    def entered_final(self) -> bool:
        return self.new != self.old and self.new in FINAL_RUN


async def recompute_run_status(
    session: AsyncSession, company_id: UUID, run: PayRun
) -> RunTransition:
    """The caller holds the run row lock (lock order 3)."""
    statuses = [PayoutStatus(s) for s in await PayoutRepo.statuses(session, company_id, run.id)]
    old, new = RunStatus(run.status), run_status_from_payouts(statuses)
    settlement_s: float | None = None
    if new != old:
        values: dict[str, Any] = {"status": str(new)}
        if new is RunStatus.PROCESSING:
            values["processing_since"] = func.now()
        if new in FINAL_RUN and run.first_final_at is None:
            values["first_final_at"] = func.now()
        await session.execute(
            update(PayRun)
            .where(
                PayRun.id == run.id,
                PayRun.status.in_(["processing", "paid", "partially_failed"]),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if "first_final_at" in values:
            row = (
                await session.execute(
                    select(PayRun.first_final_at, PayRun.approved_at).where(PayRun.id == run.id)
                )
            ).one()
            first: datetime = row.first_final_at
            approved: datetime = row.approved_at
            settlement_s = max(0.0, (first - approved).total_seconds())
    return RunTransition(
        run_id=run.id,
        old=old,
        new=new,
        paid=sum(1 for s in statuses if s is PayoutStatus.PAID),
        failed=sum(1 for s in statuses if s is PayoutStatus.FAILED),
        settlement_s=settlement_s,
    )


def report_transition(metrics: Metrics, t: RunTransition) -> None:
    """After commit: the counter on each final entry; the histogram once (``first_final_at``)."""
    if not t.entered_final:
        return
    metrics.run_outcome(str(t.new))
    if t.settlement_s is not None:
        metrics.settlement.record(t.settlement_s)
    fields: dict[str, Any] = {
        "pay_run_id": str(t.run_id),
        "status": str(t.new),
        "paid": t.paid,
        "failed": t.failed,
        "first_completion": t.settlement_s is not None,
    }
    if t.settlement_s is not None:
        fields["settlement_s"] = round(t.settlement_s, 3)
    log_event(_log, logging.INFO, "payrun.completed", **fields)

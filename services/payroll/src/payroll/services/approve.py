"""Approve a pay run (design §4.4 "Approve"; PAY-400, PAY-401, PAY-402, PAY-406).

One transaction: idempotency row (lock 1) → run ``FOR UPDATE`` (3) → employees ``FOR SHARE`` (6)
→ inserts (payouts, attempts with encrypted bank snapshots, outbox rows, audit). The run moves to
``processing``, or straight to ``paid`` when every payout is ``no_transfer``.

Dual approval (PAY-401, C73) reads the run's hours editors and the employees' unvetted bank edits
after the employee locks. A denial is audited (``payrun.approve_denied``) in its own transaction
after the rollback.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from pycommon.auth import Principal
from pycommon.db import new_id
from pycommon.logging import get_logger, log_event
from pycommon.otel import tracer
from sqlalchemy import func, insert, update

from payroll.api.schemas import PayRunOut
from payroll.crypto import employee_aad, snapshot_aad
from payroll.db.models import Payout, PayoutAttempt, PayRun
from payroll.db.repositories import (
    AuditRepo,
    DualApprovalRepo,
    EmployeeRepo,
    PayRunRepo,
    PayslipRepo,
    rowcount,
)
from payroll.db.session import session_scope
from payroll.domain import ApproverMustDiffer, EmployeeBankDetailsMissing, PayRunNotApprovable
from payroll.domain.dual_approval import check_approve_dual
from payroll.domain.state import (
    PayoutStatus,
    RunStatus,
    assert_run_approvable,
    run_status_from_payouts,
)
from payroll.services import idempotency
from payroll.services.common import decrypt_or_500, pay_run_view
from payroll.services.dual_denied import record_dual_denied
from payroll.services.runtime import Runtime

__all__ = ["ApproveResult", "approve"]

_log: Final = get_logger("payroll.pay_runs")
CHUNK: Final = 500


def _chunks[T](items: Sequence[T], size: int = CHUNK) -> Iterator[Sequence[T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


@dataclass(frozen=True)
class ApproveResult:
    status: int
    body: Any
    replayed: bool


async def approve(
    rt: Runtime, principal: Principal, run_id: UUID, idem: idempotency.IdemRequest
) -> ApproveResult:
    started = time.monotonic()
    with tracer("payroll").start_as_current_span("payroll.pay_run.approve") as span:
        span.set_attribute("payroll.pay_run_id", str(run_id))
        try:
            async with session_scope(rt.sessions, "pay_runs.approve") as session:
                replay = await idempotency.begin(session, idem)
                if replay is not None:
                    span.set_attribute("payroll.idempotent_replay", True)
                    return ApproveResult(replay.status, replay.body, replayed=True)
                span.set_attribute("payroll.idempotent_replay", False)
                out, stats = await _approve_in_tx(rt, session, principal, run_id)
                body = jsonable_encoder(out)
                await idempotency.store(session, idem, 200, body)
        except PayRunNotApprovable as exc:
            log_event(
                _log, logging.INFO, "payrun.approve_rejected", pay_run_id=str(run_id),
                code=exc.code, status=exc.run_status,
            )  # fmt: skip
            raise
        except ApproverMustDiffer as exc:
            log_event(
                _log, logging.WARNING, "dual_approval.denied", pay_run_id=str(run_id),
                action="approve", rule=exc.rule, employee_count=len(exc.employee_ids),
                actor_id=principal.sub,
            )  # fmt: skip
            await record_dual_denied(
                rt, principal, exc, action="approve", entity_id=run_id, pay_run_id=run_id
            )
            raise
        span.set_attribute("payroll.payouts", stats["payouts"])
        span.set_attribute("payroll.transfers", stats["transfers"])
    rt.wake_relay()
    _after_commit(rt, run_id=run_id, principal=principal, out=out, stats=stats, started=started)
    return ApproveResult(200, body, replayed=False)


def _after_commit(
    rt: Runtime,
    *,
    run_id: UUID,
    principal: Principal,
    out: PayRunOut,
    stats: dict[str, int],
    started: float,
) -> None:
    rt.metrics.run_outcome("approved")
    rt.metrics.payout_outcome("no_transfer", stats["no_transfer"])
    if out.status == "paid":
        rt.metrics.run_outcome("paid")
        rt.metrics.settlement.record(0.0)
        log_event(
            _log, logging.INFO, "payrun.completed", pay_run_id=str(run_id), status=out.status,
            paid=stats["payouts"], failed=0, first_completion=True, settlement_s=0,
        )  # fmt: skip
    log_event(
        _log,
        logging.INFO,
        "payrun.approved",
        pay_run_id=str(run_id),
        payouts=stats["payouts"],
        transfers=stats["transfers"],
        no_transfer=stats["no_transfer"],
        status=out.status,
        actor_id=principal.sub,
        duration_ms=round((time.monotonic() - started) * 1000, 1),
    )


async def _approve_in_tx(
    rt: Runtime, session: Any, principal: Principal, run_id: UUID
) -> tuple[PayRunOut, dict[str, int]]:
    cid = principal.cid
    run = await PayRunRepo.get(session, cid, run_id, for_update=True)
    assert_run_approvable(RunStatus(run.status))
    payslips = await PayslipRepo.for_run(session, cid, run_id)
    employees = await EmployeeRepo.for_share(
        session, cid, sorted({p.employee_id for p in payslips})
    )
    if run.require_dual_approval:
        check_approve_dual(
            require_dual_approval=True,
            approver_id=principal.sub,
            calculated_by=run.calculated_by or "",
            created_by=run.created_by,
            hours_edits=await DualApprovalRepo.hours_edits(session, cid, run_id),
            bank_edits=await DualApprovalRepo.bank_edits(
                session, cid, sorted(employees), principal.sub
            ),
        )
    missing = [
        p.employee_id
        for p in payslips
        if (p.net_cents or 0) > 0
        and (p.employee_id not in employees or employees[p.employee_id].bank_account_enc is None)
    ]
    if missing:
        raise EmployeeBankDetailsMissing(missing)

    payouts: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    transfers: list[tuple[UUID, int]] = []
    statuses: list[PayoutStatus] = []
    for p in payslips:  # ordered by id
        payout_id = new_id()
        net = p.net_cents or 0
        if net == 0:
            payouts.append(
                {
                    "id": payout_id, "company_id": cid, "pay_run_id": run_id,
                    "payslip_id": p.id, "employee_id": p.employee_id, "amount_cents": 0,
                    "status": "paid", "no_transfer": True, "current_attempt": 0,
                    "final_at": func.now(),
                }
            )  # fmt: skip
            statuses.append(PayoutStatus.PAID)
            continue
        emp = employees[p.employee_id]
        routing = decrypt_or_500(
            rt.keyring, emp.bank_routing_enc or "", aad=employee_aad("bank_routing", emp.id),
            entity="employee", entity_id=emp.id,
        )  # fmt: skip
        account = decrypt_or_500(
            rt.keyring, emp.bank_account_enc or "", aad=employee_aad("bank_account", emp.id),
            entity="employee", entity_id=emp.id,
        )  # fmt: skip
        snapshot = json.dumps({"routing": routing, "account": account}, separators=(",", ":"))
        payouts.append(
            {
                "id": payout_id, "company_id": cid, "pay_run_id": run_id, "payslip_id": p.id,
                "employee_id": p.employee_id, "amount_cents": net, "status": "requested",
                "no_transfer": False, "current_attempt": 1, "final_at": None,
            }
        )  # fmt: skip
        attempts.append(
            {
                "id": new_id(), "company_id": cid, "payout_id": payout_id, "attempt": 1,
                "status": "requested",
                "bank_snapshot_enc": rt.keyring.encrypt(snapshot, aad=snapshot_aad(payout_id, 1)),
                "account_last4": account[-4:], "requested_by": principal.sub,
                "requested_at": func.now(), "last_published_at": func.now(), "publish_count": 1,
            }
        )  # fmt: skip
        transfers.append((payout_id, net))
        statuses.append(PayoutStatus.REQUESTED)

    for chunk in _chunks(payouts):
        await session.execute(insert(Payout).values(list(chunk)))
    for chunk in _chunks(attempts):
        await session.execute(insert(PayoutAttempt).values(list(chunk)))
    for payout_id, net in transfers:
        await rt.outbox.add_payout_requested(
            session, company_id=cid, payout_id=payout_id, pay_run_id=run_id, attempt=1,
            amount_cents=net, republish_seq=0,
        )  # fmt: skip

    status = run_status_from_payouts(statuses)
    values: dict[str, Any] = {
        "status": str(status),
        "approved_by": principal.sub,
        "approved_by_name": principal.name,
        "approved_at": func.now(),
    }
    if status is RunStatus.PROCESSING:
        values["processing_since"] = func.now()
    if status is RunStatus.PAID:
        values["first_final_at"] = func.now()
    result = await session.execute(
        update(PayRun)
        .where(PayRun.id == run_id, PayRun.status == "calculated")
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if rowcount(result) != 1:  # pragma: no cover  (the row lock makes this unreachable)
        raise PayRunNotApprovable(run.status)
    stats = {
        "payouts": len(payouts),
        "transfers": len(transfers),
        "no_transfer": len(payouts) - len(transfers),
    }
    await AuditRepo.add(
        session,
        cid,
        actor_id=principal.sub,
        event="payrun.approved",
        entity_type="pay_run",
        entity_id=run_id,
        details={**stats, "status": str(status)},
    )
    return await pay_run_view(session, cid, run_id), stats

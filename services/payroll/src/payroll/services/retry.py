"""Retry a failed payout (design §4.4 "Retry"; PAY-500, PAY-406, R34, C52).

One transaction: idempotency row when a key is sent (lock 1) → run ``FOR UPDATE`` (3) → payout
``FOR UPDATE`` (4) → current attempt ``FOR UPDATE`` (5) → employee ``FOR SHARE`` (6) → inserts.
``check_retry`` (not retryable → limit → bank details), then ``check_retry_dual`` with the run's
``require_dual_approval`` snapshot (R34): the retrier is neither the employee's bank editor since
the details were last vetted, nor the operator who marked the attempt rejected (C73). A denial is
audited (``payout.retry_denied``) in its own transaction after the rollback. Attempt n+1 gets a
fresh bank snapshot under the active kid, the payout goes back to ``requested``, an outbox row is
written (``republish_seq = 0``) and the run status is recomputed (back to ``processing``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from pycommon.auth import Principal
from pycommon.db import new_id
from pycommon.logging import get_logger, log_event
from pycommon.otel import tracer
from sqlalchemy import func, insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from payroll.crypto import employee_aad, snapshot_aad
from payroll.db.models import Payout, PayoutAttempt
from payroll.db.repositories import (
    AttemptRepo,
    AuditRepo,
    DualApprovalRepo,
    EmployeeRepo,
    PayoutRepo,
    PayRunRepo,
    rowcount,
)
from payroll.db.session import session_scope
from payroll.domain import (
    ApproverMustDiffer,
    EmployeeBankDetailsMissing,
    PayoutNotRetryable,
    RetryLimitReached,
)
from payroll.domain.dual_approval import BankEdit, check_retry_dual
from payroll.domain.resolve import OPERATOR_REJECTED
from payroll.domain.retry import check_retry
from payroll.domain.state import AttemptStatus, PayoutStatus
from payroll.services import idempotency
from payroll.services.common import decrypt_or_500, payout_view
from payroll.services.dual_denied import record_dual_denied
from payroll.services.record_result import ConcurrentUpdate
from payroll.services.run_status import RunTransition, recompute_run_status, report_transition
from payroll.services.runtime import Runtime

__all__ = ["RetryResult", "retry"]

_log: Final = get_logger("payroll.payouts")


@dataclass(frozen=True)
class RetryResult:
    status: int
    body: Any
    replayed: bool


@dataclass(frozen=True)
class _Retried:
    body: Any
    pay_run_id: UUID
    attempt: int
    run: RunTransition


async def retry(
    rt: Runtime,
    principal: Principal,
    run_id: UUID,
    payout_id: UUID,
    idem: idempotency.IdemRequest | None,
) -> RetryResult:
    with tracer("payroll").start_as_current_span("payroll.payout.retry") as span:
        span.set_attribute("payroll.payout_id", str(payout_id))
        try:
            async with session_scope(rt.sessions, "payouts.retry") as session:
                if idem is not None:
                    replay = await idempotency.begin(session, idem)
                    if replay is not None:
                        return RetryResult(replay.status, replay.body, replayed=True)
                done = await _retry_in_tx(rt, session, principal, run_id, payout_id)
                if idem is not None:
                    await idempotency.store(session, idem, 200, done.body)
        except (PayoutNotRetryable, RetryLimitReached, EmployeeBankDetailsMissing) as exc:
            log_event(
                _log, logging.INFO, "payout.retry_rejected", payout_id=str(payout_id),
                code=exc.code,
            )  # fmt: skip
            raise
        except ApproverMustDiffer as exc:
            log_event(
                _log, logging.WARNING, "dual_approval.denied", pay_run_id=str(run_id),
                payout_id=str(payout_id), action="retry", rule=exc.rule,
                employee_count=len(exc.employee_ids), actor_id=principal.sub,
            )  # fmt: skip
            await record_dual_denied(
                rt, principal, exc, action="retry", entity_id=payout_id, pay_run_id=run_id
            )
            raise
        span.set_attribute("payroll.attempt", done.attempt)
    rt.wake_relay()
    log_event(
        _log, logging.INFO, "payout.retry_requested", payout_id=str(payout_id),
        pay_run_id=str(done.pay_run_id), attempt=done.attempt, actor_id=principal.sub,
    )  # fmt: skip
    report_transition(rt.metrics, done.run)
    return RetryResult(200, done.body, replayed=False)


async def _retry_in_tx(
    rt: Runtime, session: AsyncSession, principal: Principal, run_id: UUID, payout_id: UUID
) -> _Retried:
    cid = principal.cid
    run = await PayRunRepo.get(session, cid, run_id, for_update=True)
    payout = await PayoutRepo.lock_in_run(session, cid, run_id, payout_id)
    current = await AttemptRepo.lock(session, cid, payout_id, payout.current_attempt)
    employees = await EmployeeRepo.for_share(session, cid, [payout.employee_id])
    emp = employees.get(payout.employee_id)
    check_retry(
        payout_status=PayoutStatus(payout.status),
        attempt_status=AttemptStatus(current.status) if current is not None else None,
        current_attempt=payout.current_attempt,
        max_attempts=rt.settings.payout_max_attempts,
        employee_id=payout.employee_id,
        employee_active=emp is not None and emp.status == "active",
        employee_has_bank=emp is not None and emp.bank_account_enc is not None,
    )
    if emp is None:  # pragma: no cover  (check_retry refuses a missing employee)
        raise EmployeeBankDetailsMissing([payout.employee_id])
    if run.require_dual_approval:
        edits = await DualApprovalRepo.bank_edits(session, cid, [emp.id], principal.sub)
        check_retry_dual(
            require_dual_approval=True,
            retrier_id=principal.sub,
            bank_edit=edits[0] if edits else BankEdit(emp.id, None, None),
            resolved_by=(
                current.resolved_by
                if current is not None and current.failure_reason == OPERATOR_REJECTED
                else None
            ),
        )

    routing = decrypt_or_500(
        rt.keyring, emp.bank_routing_enc or "", aad=employee_aad("bank_routing", emp.id),
        entity="employee", entity_id=emp.id,
    )  # fmt: skip
    account = decrypt_or_500(
        rt.keyring, emp.bank_account_enc or "", aad=employee_aad("bank_account", emp.id),
        entity="employee", entity_id=emp.id,
    )  # fmt: skip
    attempt = payout.current_attempt + 1
    snapshot = json.dumps({"routing": routing, "account": account}, separators=(",", ":"))
    await session.execute(
        insert(PayoutAttempt).values(
            id=new_id(), company_id=cid, payout_id=payout_id, attempt=attempt,
            status="requested",
            bank_snapshot_enc=rt.keyring.encrypt(snapshot, aad=snapshot_aad(payout_id, attempt)),
            account_last4=account[-4:], requested_by=principal.sub, requested_at=func.now(),
            last_published_at=func.now(), publish_count=1,
        )
    )  # fmt: skip
    result = await session.execute(
        update(Payout)
        .where(Payout.id == payout_id, Payout.status == "failed")
        .values(status="requested", current_attempt=attempt, final_at=None)
        .execution_options(synchronize_session=False)
    )
    if rowcount(result) != 1:  # pragma: no cover  (the row locks make this unreachable)
        raise ConcurrentUpdate()
    await rt.outbox.add_payout_requested(
        session, company_id=cid, payout_id=payout_id, pay_run_id=run_id, attempt=attempt,
        amount_cents=payout.amount_cents, republish_seq=0,
    )  # fmt: skip
    transition = await recompute_run_status(session, cid, run)
    await AuditRepo.add(
        session, cid, actor_id=principal.sub, event="payout.retry_requested",
        entity_type="payout", entity_id=payout_id,
        details={"attempt": attempt, "account_last4": account[-4:]},
    )  # fmt: skip
    out = await payout_view(session, cid, payout_id)
    return _Retried(jsonable_encoder(out), run_id, attempt, transition)

"""``PayoutHandler``: the per-message algorithm of PAY-403 (design §3.1, §3.2, §5.3).

Pure orchestration over the ports ``PayrollPort``, ``BankPort``, ``DisbursementRepoPort`` and a
``Clock``. It makes no Redis call: it returns ``Ack``, ``DeadLetter`` or ``Retry`` and the
consumer executes it. The whole call runs under ``asyncio.timeout(HANDLER_DEADLINE_S)``, and a
bank POST never starts later than ``HANDLER_DEADLINE_S`` after the instruction fetch (C48).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final, Literal, Protocol
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import Link, Span, SpanKind, Status, StatusCode
from pycommon import Clock, SystemClock
from pycommon.logging import get_logger, log_event, redact
from pycommon.otel import context_from_traceparent, tracer

from payout_worker.classify import (
    BankCallResult,
    classify_lookup_response,
    classify_transfer_response,
)
from payout_worker.db.repo import DisbursementRepoPort
from payout_worker.errors import (
    AttemptNotCurrent,
    InvalidMessage,
    PayoutNotFound,
    PayrollRejected,
    PayrollUnavailable,
    WorkerInternalError,
)
from payout_worker.metrics import Metrics
from payout_worker.models import (
    FINAL_STATUSES,
    Envelope,
    Instruction,
    NoAnswer,
    NotFound,
    Outcome,
    Rejected,
    ResultRequest,
    ResultResponse,
    Settled,
    StreamMessage,
    TransferRequest,
    parse_envelope,
)
from payout_worker.settings import Settings

__all__ = [
    "Ack",
    "BankPort",
    "DeadLetter",
    "ErrorKind",
    "HandleResult",
    "PayoutHandler",
    "PayrollPort",
    "Retry",
]

_log: Final = get_logger("payout_worker.handler")
SPAN_NAME: Final = "payout.requested process"
MAX_ERROR_CHARS: Final = 500
_IDEM_SAFE: Final = re.compile(r"^[A-Za-z0-9._:-]{1,190}$")

ErrorKind = Literal["invalid", "payroll", "internal", "bank"]


@dataclass(frozen=True)
class Ack:
    outcome: str


@dataclass(frozen=True)
class DeadLetter:
    kind: Literal["invalid", "payroll", "internal"]
    code: str
    error: str


@dataclass(frozen=True)
class Retry:
    kind: ErrorKind
    code: str
    error: str


HandleResult = Ack | DeadLetter | Retry


class PayrollPort(Protocol):
    async def get_instruction(
        self, payout_id: UUID, attempt: int, company_id: UUID, *, request_id: str | None = None
    ) -> Instruction: ...

    async def post_result(
        self,
        payout_id: UUID,
        company_id: UUID,
        body: ResultRequest,
        idempotency_key: str,
        *,
        request_id: str | None = None,
    ) -> ResultResponse: ...


class BankPort(Protocol):
    async def create_transfer(self, key: str, body: TransferRequest) -> BankCallResult: ...

    async def get_transfer(self, key: str) -> BankCallResult: ...


def error_text(exc: BaseException | type[BaseException], code: str) -> str:
    """The DLQ ``error``: class name and the fixed code, redacted, ≤ 500 characters (C21)."""
    name = exc.__name__ if isinstance(exc, type) else type(exc).__name__
    text = redact(f"{name}: {code}")
    return str(text)[:MAX_ERROR_CHARS]


def _retry(kind: ErrorKind, exc: BaseException | type[BaseException], code: str) -> Retry:
    return Retry(kind, code, error_text(exc, code))


def _dead(
    kind: Literal["invalid", "payroll", "internal"],
    exc: BaseException | type[BaseException],
    code: str,
) -> DeadLetter:
    return DeadLetter(kind, code, error_text(exc, code))


class PayoutHandler:
    def __init__(
        self,
        settings: Settings,
        repo: DisbursementRepoPort,
        payroll: PayrollPort,
        bank: BankPort,
        metrics: Metrics,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._s = settings
        self._repo = repo
        self._payroll = payroll
        self._bank = bank
        self._metrics = metrics
        self._clock = clock or SystemClock()

    # --- entry point ---------------------------------------------------------------------------

    async def handle(self, msg: StreamMessage, delivery: int) -> HandleResult:
        try:
            env = parse_envelope(msg)
        except InvalidMessage as exc:
            return await self._invalid(msg, exc, delivery)
        d = env.data
        fields: dict[str, Any] = {
            "event_id": str(env.event_id),
            "payout_id": str(d.payout_id),
            "attempt": d.attempt,
            "stream_id": msg.id,
        }
        with self._consume_span(msg, delivery, env.traceparent, fields) as span:
            result = await self._guarded(env, delivery, fields)
            self._end_span(span, result)
            return result

    async def _guarded(self, env: Envelope, delivery: int, lf: dict[str, Any]) -> HandleResult:
        try:
            async with asyncio.timeout(self._s.handler_deadline_s):
                return await self._process(env, delivery, lf)
        except TimeoutError:
            return _retry("internal", WorkerInternalError, "handler_deadline")
        except (PayrollUnavailable, PayrollRejected) as exc:
            return _retry("payroll", exc, exc.code)
        except WorkerInternalError as exc:
            return _retry("internal", exc, exc.code)
        except Exception as exc:
            log_event(
                _log,
                logging.ERROR,
                "payout.handler.unexpected",
                error_type=type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
                **lf,
            )
            return _retry("internal", exc, "unexpected")

    # --- PAY-403 steps 1-8 ---------------------------------------------------------------------

    async def _process(self, env: Envelope, delivery: int, lf: dict[str, Any]) -> HandleResult:
        early = await self._steps_1_to_4(env, lf)
        if not isinstance(early, tuple):
            return early
        ins, fetched_at = early
        answer = await self._steps_5_6(env, ins, fetched_at, delivery, lf)
        if isinstance(answer, Ack | DeadLetter | Retry):
            return answer
        return await self._steps_7_8(env, answer, delivery, lf)

    async def _steps_1_to_4(
        self, env: Envelope, lf: dict[str, Any]
    ) -> HandleResult | tuple[Instruction, float]:
        d = env.data
        if await self._repo.is_processed(env.event_id):  # step 1
            return Ack("duplicate")
        await self._repo.upsert_submitting(env)  # step 2
        with _child("payout.instruction"):  # step 3, on every delivery
            try:
                ins = await self._payroll.get_instruction(
                    d.payout_id, d.attempt, d.company_id, request_id=str(env.event_id)
                )
            except AttemptNotCurrent:
                await self._repo.finish(env, "attempt_not_current")
                log_event(
                    _log, logging.INFO, "payout.attempt_not_current", source="instruction", **lf
                )
                return Ack("attempt_not_current")
            except PayoutNotFound as exc:
                return _dead("invalid", exc, "payout_not_found")
        fetched_at = self._clock.monotonic()
        if (  # step 4: the instruction's amount, never the event's
            ins.amount_cents != d.amount_cents
            or ins.currency != d.currency
            or ins.payout_id != d.payout_id
            or ins.attempt != d.attempt
        ):
            await self._report_poison(env, "amount_mismatch", lf)
            await self._repo.record_answer(
                d.payout_id, d.attempt, "poison", "amount_mismatch", None
            )
            return _dead("invalid", InvalidMessage, "amount_mismatch")
        return ins, fetched_at

    async def _steps_5_6(
        self,
        env: Envelope,
        ins: Instruction,
        fetched_at: float,
        delivery: int,
        lf: dict[str, Any],
    ) -> HandleResult | Settled | Rejected | NoAnswer:
        key = env.bank_key
        window = timedelta(seconds=self._s.bank_key_safe_window_s)
        if self._clock.now() - ins.requested_at > window:  # step 5 (PAY-410): GET only
            looked = await self._bank_call("get", key, None, delivery, lf)
            if not isinstance(looked, NotFound):
                return looked
            await self._report_poison(env, "key_window_expired", lf)
            await self._repo.finish(env, "poison", reason="key_window_expired")
            log_event(
                _log,
                logging.ERROR,
                "payout.key_window_expired",
                requested_at=ins.requested_at.isoformat(),
                **lf,
            )
            return Ack("poison")
        # step 6; never start the POST later than HANDLER_DEADLINE_S after the fetch (C48)
        if self._clock.monotonic() - fetched_at > self._s.handler_deadline_s:
            return _retry("internal", WorkerInternalError, "handler_deadline")
        body = TransferRequest(
            amount_cents=ins.amount_cents,
            currency=ins.currency,
            routing=ins.routing.get_secret_value(),
            account=ins.account.get_secret_value(),
        )
        created = await self._bank_call("create", key, body, delivery, lf)
        if isinstance(created, NotFound):  # pragma: no cover  (a POST is never NotFound)
            return NoAnswer("http_404", 404)
        return created

    async def _steps_7_8(
        self,
        env: Envelope,
        answer: Settled | Rejected | NoAnswer,
        delivery: int,
        lf: dict[str, Any],
    ) -> HandleResult:
        d = env.data
        outcome: Outcome
        reason: str | None = None
        bank_reference: str | None = None
        if isinstance(answer, NoAnswer):
            if delivery < self._s.payout_max_deliveries:
                return Retry("bank", answer.reason, f"BankNoAnswer: {answer.reason}")
            log_event(
                _log,
                logging.WARNING,
                "payout.indeterminate",
                last_reason=answer.reason,
                delivery=delivery,
                **lf,
            )
            outcome = "indeterminate"  # never rejected, never failed (PAY-404)
        elif isinstance(answer, Settled):
            outcome, bank_reference = "settled", answer.bank_reference
        else:
            outcome, reason = "rejected", answer.reason
        await self._repo.record_answer(d.payout_id, d.attempt, outcome, reason, bank_reference)
        await self._report(env, outcome, reason, bank_reference, lf)  # step 7
        with _child("payout.finish"):  # step 8.1; the consumer XACKs after we return
            await self._repo.finish(env, outcome, reason, bank_reference)
        return Ack(outcome)

    # --- bank ----------------------------------------------------------------------------------

    async def _bank_call(
        self,
        op: Literal["create", "get"],
        key: str,
        body: TransferRequest | None,
        delivery: int,
        lf: dict[str, Any],
    ) -> Settled | Rejected | NoAnswer | NotFound:
        with _child("payout.bank_call", {"payrun.bank_operation": op}) as span:
            if op == "create" and body is not None:
                result = await self._bank.create_transfer(key, body)
                answer: Settled | Rejected | NoAnswer | NotFound = classify_transfer_response(
                    result
                )
            else:
                answer = classify_lookup_response(await self._bank.get_transfer(key))
            operation = "create" if op == "create" else "get"
            if isinstance(answer, Settled):
                span.set_attribute("payrun.bank_answer", "settled")
                self._metrics.bank_transfer("settled", "none")
                log_event(
                    _log,
                    logging.INFO,
                    "bank.transfer.submitted",
                    transfer_id=str(answer.transfer_id),
                    operation=operation,
                    bank_reference=answer.bank_reference,
                    **lf,
                )
            elif isinstance(answer, Rejected):
                span.set_attribute("payrun.bank_answer", "rejected")
                self._metrics.bank_transfer("rejected", answer.reason)
                log_event(
                    _log,
                    logging.INFO,
                    "bank.transfer.rejected",
                    transfer_id=str(answer.transfer_id),
                    reason=answer.reason,
                    operation=operation,
                    **lf,
                )
            elif isinstance(answer, NotFound):
                span.set_attribute("payrun.bank_answer", "not_found")
                self._metrics.bank_transfer("not_found", "none")
            else:
                span.set_attribute("payrun.bank_answer", "no_answer")
                self._metrics.bank_transfer("no_answer", answer.reason)
                log_event(
                    _log,
                    logging.WARNING,
                    "bank.transfer.no_answer",
                    reason=answer.reason,
                    http_status=answer.http_status,
                    operation=operation,
                    delivery=delivery,
                    **lf,
                )
            return answer

    # --- payroll result ------------------------------------------------------------------------

    async def _report(
        self,
        env: Envelope,
        outcome: Outcome,
        reason: str | None,
        bank_reference: str | None,
        lf: dict[str, Any],
    ) -> None:
        """Step 7: ``Idempotency-Key = <event_id>:<outcome>``; any 200 and a 409 are final."""
        d = env.data
        with _child("payout.report_result", {"payrun.outcome": outcome}) as span:
            try:
                res = await self._payroll.post_result(
                    d.payout_id,
                    d.company_id,
                    ResultRequest(
                        attempt=d.attempt,
                        outcome=outcome,
                        reason=reason,
                        bank_reference=bank_reference,
                    ),
                    idempotency_key=f"{env.event_id}:{outcome}",
                    request_id=str(env.event_id),
                )
            except AttemptNotCurrent:
                span.set_attribute("payrun.stored_outcome", "attempt_not_current")
                log_event(_log, logging.INFO, "payout.attempt_not_current", source="result", **lf)
                return
            span.set_attribute("payrun.stored_outcome", res.outcome)
            if res.outcome in FINAL_STATUSES and res.outcome != outcome:
                log_event(
                    _log,
                    logging.INFO,
                    "payout.result_already_final",
                    posted=outcome,
                    stored=res.outcome,
                    **lf,
                )

    async def _post_poison(
        self, payout_id: UUID, company_id: UUID, attempt: int, reason: str, event_id: str
    ) -> None:
        # A 409 counts as delivered, like a 200 with any stored outcome.
        with contextlib.suppress(AttemptNotCurrent):
            await self._payroll.post_result(
                payout_id,
                company_id,
                ResultRequest(attempt=attempt, outcome="poison", reason=reason),
                idempotency_key=f"{event_id}:poison",
                request_id=event_id,
            )

    async def _report_poison(self, env: Envelope, reason: str, lf: dict[str, Any]) -> None:
        d = env.data
        with _child("payout.report_result", {"payrun.outcome": "poison"}):
            await self._post_poison(d.payout_id, d.company_id, d.attempt, reason, str(env.event_id))
        log_event(_log, logging.ERROR, "payout.poison_reported", reason=reason, **lf)

    # --- step 0: invalid messages --------------------------------------------------------------

    async def _invalid(
        self, msg: StreamMessage, exc: InvalidMessage, delivery: int
    ) -> HandleResult:
        lf: dict[str, Any] = {
            "event_id": str(exc.event_id) if exc.event_id else None,
            "payout_id": str(exc.payout_id) if exc.payout_id else None,
            "attempt": exc.attempt,
            "stream_id": msg.id,
        }
        if exc.traceparent is None:
            log_event(_log, logging.WARNING, "queue.message.trace_missing", stream_id=msg.id)
        with self._consume_span(msg, delivery, exc.traceparent, lf) as span:
            result = await self._invalid_result(msg, exc, lf)
            self._end_span(span, result)
            return result

    async def _invalid_result(
        self, msg: StreamMessage, exc: InvalidMessage, lf: dict[str, Any]
    ) -> HandleResult:
        if exc.payout_id is not None and exc.company_id is not None and exc.attempt is not None:
            event_id = self._poison_event_id(msg, exc)
            try:
                with _child("payout.report_result", {"payrun.outcome": "poison"}):
                    await self._post_poison(
                        exc.payout_id, exc.company_id, exc.attempt, "schema_invalid", event_id
                    )
            except (PayrollUnavailable, PayrollRejected) as err:
                return _retry("payroll", err, err.code)
            except Exception as err:
                return _retry("internal", err, "unexpected")
            log_event(_log, logging.ERROR, "payout.poison_reported", reason="schema_invalid", **lf)
        return _dead("invalid", exc, "schema_invalid")

    @staticmethod
    def _poison_event_id(msg: StreamMessage, exc: InvalidMessage) -> str:
        if exc.event_id is not None:
            return str(exc.event_id)
        raw = msg.field("event_id")
        text = raw.decode(errors="replace") if raw is not None else ""
        return text if _IDEM_SAFE.match(text) else msg.id

    # --- spans ---------------------------------------------------------------------------------

    @contextmanager
    def _consume_span(
        self, msg: StreamMessage, delivery: int, traceparent: str | None, lf: dict[str, Any]
    ) -> Iterator[Span]:
        """``payout.requested process`` (CONSUMER), a child of the envelope's traceparent."""
        parent = context_from_traceparent(traceparent)
        parent_ctx = trace.get_current_span(parent).get_span_context()
        links = [Link(parent_ctx)] if parent_ctx.is_valid else []
        attributes: dict[str, Any] = {
            "messaging.system": "redis",
            "messaging.destination.name": self._s.payout_stream,
            "messaging.consumer.group.name": self._s.payout_group,
            "messaging.message.id": msg.id,
            "payrun.delivery": delivery,
        }
        for key in ("event_id", "payout_id", "attempt"):
            if lf.get(key) is not None:
                attributes[f"payrun.{key}"] = lf[key]
        with tracer("payout_worker").start_as_current_span(
            SPAN_NAME,
            context=parent,
            kind=SpanKind.CONSUMER,
            links=links,
            attributes=attributes,
        ) as span:
            yield span

    @staticmethod
    def _end_span(span: Span, result: HandleResult) -> None:
        if isinstance(result, Ack):
            span.set_attribute("payrun.outcome", result.outcome)
            return
        span.set_attribute("payrun.error_code", result.code)
        span.add_event(
            "exception", {"exception.type": result.kind, "exception.message": result.error}
        )
        span.set_status(Status(StatusCode.ERROR, result.code))


@contextmanager
def _child(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Span]:
    with tracer("payout_worker").start_as_current_span(name, attributes=attributes or {}) as span:
        yield span

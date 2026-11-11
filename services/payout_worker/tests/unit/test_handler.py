"""Every branch of the handler algorithm with fakes (design §3.1; PAY-403, 404, 405, 408, 409,
410, C26, C48)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from payout_worker.errors import (
    AttemptNotCurrent,
    PayoutNotFound,
    PayrollRejected,
    PayrollUnavailable,
    WorkerInternalError,
)
from payout_worker.handler import Ack, DeadLetter, PayoutHandler, Retry
from payout_worker.models import ResultResponse
from pycommon.testing import FakeClock, JsonLogCapture

from tests.helpers import (
    BANK_REF,
    COMPANY_ID,
    EVENT_ID,
    KEY,
    PAYOUT_ID,
    FakeBank,
    FakePayroll,
    FakeRepo,
    bank_response,
    envelope_dict,
    instruction,
    make_metrics,
    make_settings,
    message,
    requested_at,
    transfer_json,
)


@dataclass
class H:
    handler: PayoutHandler
    repo: FakeRepo
    payroll: FakePayroll
    bank: FakeBank
    clock: FakeClock


@pytest.fixture
def h() -> H:
    clock = FakeClock()
    repo, payroll, bank = FakeRepo(), FakePayroll(clock=clock), FakeBank()
    s = make_settings()
    handler = PayoutHandler(s, repo, payroll, bank, make_metrics(s), clock=clock)
    return H(handler, repo, payroll, bank, clock)


async def test_settled_happy_path_in_order(h: H) -> None:
    """PAY-403: upsert, instruction, POST with <payout_id>:<attempt>, result with
    <event_id>:settled, then finish; Ack(settled)."""
    result = await h.handler.handle(message(), 1)
    assert result == Ack("settled")
    assert h.repo.names() == [
        "is_processed",
        "upsert_submitting",
        "record_answer",
        "finish",
    ]
    assert h.repo.last("finish") == ("settled", None, BANK_REF)
    [(key, body)] = h.bank.create_calls
    assert key == KEY
    assert body.amount_cents == 412_345
    [(pid, cid, req, idem)] = h.payroll.result_calls
    assert (pid, cid, req.outcome, req.bank_reference) == (
        PAYOUT_ID,
        COMPANY_ID,
        "settled",
        BANK_REF,
    )
    assert idem == f"{EVENT_ID}:settled"


async def test_rejected_definite(h: H) -> None:
    """PAY-404: a 2xx rejected with a reason → rejected posted with the reason."""
    h.bank.creates = [bank_response(201, transfer_json("rejected"))]
    assert await h.handler.handle(message(), 1) == Ack("rejected")
    req = h.payroll.result_calls[0][2]
    assert (req.outcome, req.reason) == ("rejected", "account_closed")
    assert h.payroll.result_calls[0][3] == f"{EVENT_ID}:rejected"


async def test_processed_event_is_skipped(h: H) -> None:
    """PAY-403 step 1: a processed event_id → Ack(duplicate), no DB write, no HTTP call."""
    h.repo.processed.add(EVENT_ID)
    assert await h.handler.handle(message(), 2) == Ack("duplicate")
    assert h.repo.names() == ["is_processed"]
    assert h.payroll.instruction_calls == []
    assert h.bank.create_calls == []


async def test_attempt_not_current_no_bank_call(h: H, caplog_json: JsonLogCapture) -> None:
    """PAY-403 step 3: 409 attempt_not_current → finish, Ack, no bank call."""
    h.payroll.instructions = [AttemptNotCurrent()]
    assert await h.handler.handle(message(), 1) == Ack("attempt_not_current")
    assert h.repo.last("finish") == ("attempt_not_current", None, None)
    assert h.bank.create_calls == []
    assert caplog_json.events("payout.attempt_not_current")[0]["source"] == "instruction"


async def test_payout_not_found_dead_letters_without_report(h: H) -> None:
    """R5, PAY-408: payout_not_found → DLQ at once on delivery 1, no poison report."""
    h.payroll.instructions = [PayoutNotFound()]
    result = await h.handler.handle(message(), 1)
    assert isinstance(result, DeadLetter)
    assert (result.kind, result.code) == ("invalid", "payout_not_found")
    assert h.payroll.result_calls == []


@pytest.mark.parametrize(
    "change",
    [{"amount_cents": 1}, {"attempt": 2}, {"payout_id": "0193f1d2-6c4a-7000-8000-00000000aaaa"}],
)
async def test_mismatch_is_poison(
    h: H, change: dict[str, Any], caplog_json: JsonLogCapture
) -> None:
    """PAY-409, R5: amount/attempt/payout mismatch → poison amount_mismatch reported, DLQ at
    once, no bank call; amounts never logged."""
    h.payroll.instructions = [instruction(**change)]
    result = await h.handler.handle(message(), 1)
    assert isinstance(result, DeadLetter)
    assert (result.kind, result.code) == ("invalid", "amount_mismatch")
    req = h.payroll.result_calls[0][2]
    assert (req.outcome, req.reason) == ("poison", "amount_mismatch")
    assert h.payroll.result_calls[0][3] == f"{EVENT_ID}:poison"
    assert h.repo.last("record_answer") == ("poison", "amount_mismatch", None)
    assert h.bank.create_calls == []
    assert caplog_json.events("payout.poison_reported")
    assert "412345" not in caplog_json.text


async def test_poison_report_409_still_dead_letters(h: H) -> None:
    """PAY-409: a 409 on the poison report counts as delivered."""
    h.payroll.instructions = [instruction(amount_cents=5)]
    h.payroll.results = [AttemptNotCurrent()]
    assert isinstance(await h.handler.handle(message(), 1), DeadLetter)


async def test_poison_report_payroll_down_retries(h: H) -> None:
    """Design §3.2: a failed poison report follows the payroll rules (leave pending)."""
    h.payroll.instructions = [instruction(amount_cents=5)]
    h.payroll.results = [PayrollUnavailable()]
    result = await h.handler.handle(message(), 1)
    assert isinstance(result, Retry)
    assert (result.kind, result.code) == ("payroll", "payroll_unavailable")


async def test_schema_invalid_with_readable_ids(h: H, caplog_json: JsonLogCapture) -> None:
    """R5, PAY-409: schema-invalid with readable IDs → poison schema_invalid reported, then
    DeadLetter at once on delivery 1; no DB write."""
    env = envelope_dict(amount_cents=0)
    result = await h.handler.handle(message(env), 1)
    assert isinstance(result, DeadLetter)
    assert (result.kind, result.code) == ("invalid", "schema_invalid")
    [(pid, cid, req, idem)] = h.payroll.result_calls
    assert (pid, cid, req.outcome, req.reason) == (
        PAYOUT_ID,
        COMPANY_ID,
        "poison",
        "schema_invalid",
    )
    assert idem == f"{EVENT_ID}:poison"
    assert h.repo.calls == []


async def test_schema_invalid_unreadable_ids_dlq_only(h: H, caplog_json: JsonLogCapture) -> None:
    """R5: unreadable IDs → DLQ only; a missing traceparent logs queue.message.trace_missing."""
    for msg in (
        message(raw=b"not json"),
        message(drop_envelope=True),
        message(raw=json.dumps({"data": {"payout_id": "x"}}).encode()),
    ):
        result = await h.handler.handle(msg, 1)
        assert isinstance(result, DeadLetter)
        assert result.code == "schema_invalid"
    assert h.payroll.result_calls == []
    assert len(caplog_json.events("queue.message.trace_missing")) == 3


async def test_schema_invalid_event_id_fallbacks(h: H) -> None:
    """Design §3.1 step 0: event_id from the envelope, else the entry's field, else the entry ID."""
    env = envelope_dict(currency="EUR")
    del env["event_id"]
    await h.handler.handle(message(env, event_id_field="0193f1d2-6c4a-7b1e-9a3f-000000000099"), 1)
    await h.handler.handle(message(env, event_id_field="bad id!", entry_id="5-1"), 1)
    keys = [c[3] for c in h.payroll.result_calls]
    assert keys == ["0193f1d2-6c4a-7b1e-9a3f-000000000099:poison", "5-1:poison"]


@pytest.mark.parametrize(
    ("exc", "kind"),
    [(PayrollUnavailable(), "payroll"), (PayrollRejected("invalid_token"), "payroll")],
)
async def test_schema_invalid_report_failure(h: H, exc: Exception, kind: str) -> None:
    """Design §3.2: a failed schema_invalid report leaves the message pending."""
    h.payroll.results = [exc]  # type: ignore[list-item]
    result = await h.handler.handle(message(envelope_dict(currency="EUR")), 1)
    assert isinstance(result, Retry)
    assert result.kind == kind


async def test_schema_invalid_report_unexpected(h: H, monkeypatch: pytest.MonkeyPatch) -> None:
    """Design §3.2: an unexpected error in the poison report → Retry(internal, unexpected)."""

    async def boom(*_a: Any, **_k: Any) -> Any:
        raise ValueError("x")

    monkeypatch.setattr(h.payroll, "post_result", boom)
    result = await h.handler.handle(message(envelope_dict(currency="EUR")), 1)
    assert result == Retry("internal", "unexpected", "ValueError: unexpected")


@pytest.mark.parametrize(
    ("lookup", "expected"),
    [
        (bank_response(200), Ack("settled")),
        (bank_response(200, transfer_json("rejected")), Ack("rejected")),
    ],
)
async def test_key_window_found(h: H, lookup: Any, expected: Ack) -> None:
    """PAY-410: older than the window → GET only; a found transfer is the answer; no POST."""
    h.payroll.instructions = [
        instruction(requested_at=requested_at(timedelta(hours=25)).isoformat())
    ]
    h.bank.lookups = [lookup]
    assert await h.handler.handle(message(), 1) == expected
    assert h.bank.get_calls == [KEY]
    assert h.bank.create_calls == []


async def test_key_window_not_found_is_poison(h: H, caplog_json: JsonLogCapture) -> None:
    """PAY-410: GET 404 → poison key_window_expired reported, finish, Ack(poison); never POST."""
    h.payroll.instructions = [
        instruction(requested_at=requested_at(timedelta(hours=25)).isoformat())
    ]
    assert await h.handler.handle(message(), 1) == Ack("poison")
    req = h.payroll.result_calls[0][2]
    assert (req.outcome, req.reason) == ("poison", "key_window_expired")
    assert h.repo.last("finish") == ("poison", "key_window_expired", None)
    assert h.bank.create_calls == []
    assert caplog_json.events("payout.key_window_expired")


async def test_key_window_no_answer_retries(h: H) -> None:
    """PAY-410: GET with no answer follows the no-answer rule."""
    h.payroll.instructions = [
        instruction(requested_at=requested_at(timedelta(hours=25)).isoformat())
    ]
    h.bank.lookups = [bank_response(503, {})]
    assert await h.handler.handle(message(), 1) == Retry(
        "bank", "http_5xx", "BankNoAnswer: http_5xx"
    )


async def test_no_answer_below_budget_retries(h: H, caplog_json: JsonLogCapture) -> None:
    """PAY-404: no answer below PAYOUT_MAX_DELIVERIES → Retry(bank); nothing posted."""
    h.bank.creates = [bank_response(500, {})]
    result = await h.handler.handle(message(), 4)
    assert result == Retry("bank", "http_5xx", "BankNoAnswer: http_5xx")
    assert h.payroll.result_calls == []
    assert caplog_json.events("bank.transfer.no_answer")[0]["delivery"] == 4


async def test_no_answer_at_budget_is_indeterminate(h: H, caplog_json: JsonLogCapture) -> None:
    """PAY-404, PAY-408: at the budget → indeterminate posted (never rejected), finish, Ack."""
    h.bank.creates = [bank_response(409, transfer_json("rejected"))]
    assert await h.handler.handle(message(), 5) == Ack("indeterminate")
    req = h.payroll.result_calls[0][2]
    assert (req.outcome, req.reason, req.bank_reference) == ("indeterminate", None, None)
    assert h.payroll.result_calls[0][3] == f"{EVENT_ID}:indeterminate"
    assert caplog_json.events("payout.indeterminate")[0]["last_reason"] == "http_409"


async def test_result_409_is_final(h: H, caplog_json: JsonLogCapture) -> None:
    """Plan J4: a 409 on the result is a late result for an old attempt → finish and Ack."""
    h.payroll.results = [AttemptNotCurrent()]
    assert await h.handler.handle(message(), 1) == Ack("settled")
    assert h.repo.last("finish") == ("settled", None, BANK_REF)
    assert caplog_json.events("payout.attempt_not_current")[0]["source"] == "result"


@pytest.mark.parametrize(
    ("stored", "logged"),
    [
        ("requested", False),
        ("indeterminate", False),
        ("needs_operator", False),
        ("settled", False),
        ("rejected", True),
    ],
)
async def test_result_any_stored_status(
    h: H, caplog_json: JsonLogCapture, stored: str, logged: bool
) -> None:
    """C26: any 200 is success; payout.result_already_final only for a differing final status."""
    h.payroll.results = [
        ResultResponse.model_validate({"payout_id": PAYOUT_ID, "attempt": 1, "outcome": stored})
    ]
    assert await h.handler.handle(message(), 1) == Ack("settled")
    events = caplog_json.events("payout.result_already_final")
    assert bool(events) is logged
    if logged:
        assert (events[0]["posted"], events[0]["stored"]) == ("settled", "rejected")


@pytest.mark.parametrize(
    ("exc", "kind", "code"),
    [
        (PayrollUnavailable(), "payroll", "payroll_unavailable"),
        (PayrollUnavailable("breaker_open"), "payroll", "breaker_open"),
        (PayrollRejected("invalid_token"), "payroll", "invalid_token"),
    ],
)
async def test_payroll_failures_retry(h: H, exc: Exception, kind: Any, code: str) -> None:
    """Design §3.2: payroll failures on the instruction or the result → Retry(payroll)."""
    h.payroll.instructions = [exc]  # type: ignore[list-item]
    expected = Retry(kind, code, f"{type(exc).__name__}: {code}")
    assert (await h.handler.handle(message(), 1)) == expected
    h.payroll.results = [exc]  # type: ignore[list-item]
    result = await h.handler.handle(message(), 2)
    assert isinstance(result, Retry)
    assert result.code == code
    assert "finish" not in h.repo.names()


async def test_db_failure_retries_internal(h: H) -> None:
    """Design §3.2: a DB error or timeout → Retry(internal, db_timeout)."""
    h.repo.fail = WorkerInternalError("db_timeout")
    assert await h.handler.handle(message(), 1) == Retry(
        "internal", "db_timeout", "WorkerInternalError: db_timeout"
    )


async def test_unexpected_error_retries_and_logs(h: H, caplog_json: JsonLogCapture) -> None:
    """Design §3.2: an unexpected exception is logged with its traceback and left pending."""
    h.repo.fail = ValueError("boom")  # type: ignore[assignment]
    result = await h.handler.handle(message(), 1)
    assert result == Retry("internal", "unexpected", "ValueError: unexpected")
    [event] = caplog_json.events("payout.handler.unexpected")
    assert "Traceback" in event["exc"]


async def test_handler_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Design §3.1: the whole call runs under HANDLER_DEADLINE_S → Retry(internal)."""
    s = make_settings(bank_total_deadline_s=0.01, payout_payroll_call_deadline_s=0.01)
    monkeypatch.setattr(type(s), "handler_deadline_s", property(lambda _self: 0.01))
    payroll = FakePayroll()

    async def slow(*_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(1)

    monkeypatch.setattr(payroll, "get_instruction", slow)
    handler = PayoutHandler(s, FakeRepo(), payroll, FakeBank(), make_metrics(s))
    assert await handler.handle(message(), 1) == Retry(
        "internal", "handler_deadline", "WorkerInternalError: handler_deadline"
    )


class _JumpClock(FakeClock):
    """Time jumps 36 s between the instruction fetch and the POST check."""

    def now(self) -> Any:
        self.advance(36)
        return super().now()


async def test_c48_never_post_late() -> None:
    """C48: no bank POST starts later than HANDLER_DEADLINE_S after the instruction fetch."""
    s = make_settings()
    bank = FakeBank()
    handler = PayoutHandler(s, FakeRepo(), FakePayroll(), bank, make_metrics(s), clock=_JumpClock())
    result = await handler.handle(message(), 1)
    assert result == Retry("internal", "handler_deadline", "WorkerInternalError: handler_deadline")
    assert bank.create_calls == []

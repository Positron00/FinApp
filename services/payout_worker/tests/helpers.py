"""Helpers shared by the payout-worker suites."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from payout_worker.classify import BankCallResult
from payout_worker.db.repo import StoredAnswer
from payout_worker.errors import WorkerError
from payout_worker.metrics import Metrics
from payout_worker.models import (
    Envelope,
    Instruction,
    ResultRequest,
    ResultResponse,
    StreamMessage,
    TransferRequest,
)
from payout_worker.settings import Settings
from pycommon.testing import FAKE_EPOCH, TokenFactory
from pydantic import SecretStr

COMPANY_ID = UUID("0193a000-0000-7000-8000-000000000001")
PAYOUT_ID = UUID("0193f1d2-6c4a-7000-8000-000000000001")
PAY_RUN_ID = UUID("0193f1d2-6c4a-7000-8000-000000000002")
EVENT_ID = UUID("0193f1d2-6c4a-7b1e-9a3f-2c5d8e9f0a1b")
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_SPAN_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01"
AMOUNT = 412_345
ROUTING = "021000021"
ACCOUNT = "000123456789"
# Built at runtime so no literal looks like a credential (testing.md §9).
API_KEY = secrets.token_hex(20)
STRONG_SECRET = secrets.token_urlsafe(24)
BANK_REF = "BSR-0123456789ABCDEF"
KEY = f"{PAYOUT_ID}:1"
NOT_FOUND_BODY = {"error": {"code": "transfer_not_found", "message": "No transfer."}}

TOKENS = TokenFactory.create()


def make_settings(**overrides: Any) -> Settings:
    """Settings built without reading env or ``.env`` (``model_validate`` skips the sources)."""
    values: dict[str, Any] = {
        "company_id": COMPANY_ID,
        "db_password": SecretStr(STRONG_SECRET),
        "redis_password": SecretStr(STRONG_SECRET + "-redis"),
        "payout_worker_jwt_private_key": SecretStr(TOKENS.private_pems["payout-worker"]),
        "payout_worker_jwt_kid": TOKENS.kids["payout-worker"],
        "bank_stub_api_key": SecretStr(API_KEY),
        "payout_consumer_name": "worker-test-1",
    }
    values.update(overrides)
    return Settings.model_validate(values)


def envelope_dict(
    *,
    event_id: UUID = EVENT_ID,
    traceparent: str = TRACEPARENT,
    **data: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "payout_id": str(PAYOUT_ID),
        "pay_run_id": str(PAY_RUN_ID),
        "company_id": str(COMPANY_ID),
        "attempt": 1,
        "amount_cents": AMOUNT,
        "currency": "USD",
        "republish_seq": 0,
    }
    body.update(data)
    return {
        "event_id": str(event_id),
        "type": "payout.requested",
        "version": 1,
        "occurred_at": "2026-11-10T14:03:07.123Z",
        "producer": "payroll",
        "traceparent": traceparent,
        "data": body,
    }


def envelope(**kw: Any) -> Envelope:
    return Envelope.model_validate(envelope_dict(**kw))


def message(
    env: dict[str, Any] | None = None,
    *,
    entry_id: str = "1700000000000-0",
    raw: bytes | None = None,
    event_id_field: str | None = None,
    drop_envelope: bool = False,
) -> StreamMessage:
    doc = env if env is not None else envelope_dict()
    fields: dict[bytes | str, bytes | str] = {
        b"event_id": (event_id_field or str(doc.get("event_id", ""))).encode(),
        b"type": b"payout.requested",
    }
    if not drop_envelope:
        fields[b"envelope"] = raw if raw is not None else json.dumps(doc).encode()
    return StreamMessage(entry_id, fields)


def instruction(**over: Any) -> Instruction:
    values: dict[str, Any] = {
        "payout_id": str(PAYOUT_ID),
        "attempt": 1,
        "amount_cents": AMOUNT,
        "currency": "USD",
        "routing": ROUTING,
        "account": ACCOUNT,
        "requested_at": (FAKE_EPOCH - timedelta(minutes=1)).isoformat(),
    }
    values.update(over)
    return Instruction.model_validate(values)


def transfer_json(status: str = "settled", **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "transfer_id": "0193f1d2-6c4a-7000-8000-0000000000ff",
        "status": status,
        "reason": None if status == "settled" else "account_closed",
        "bank_reference": BANK_REF if status == "settled" else None,
    }
    body.update(over)
    return body


def bank_response(
    status_code: int = 201, body: Any = None, *, raw: bytes | None = None
) -> BankCallResult:
    if raw is not None:
        return BankCallResult(httpx.Response(status_code, content=raw))
    return BankCallResult(
        httpx.Response(status_code, json=transfer_json() if body is None else body)
    )


@dataclass
class FakePayroll:
    """``PayrollPort`` with scripted answers (an exception is raised, a value returned)."""

    instructions: list[Instruction | WorkerError] = field(default_factory=list)
    results: list[ResultResponse | WorkerError | str] = field(default_factory=list)
    instruction_calls: list[tuple[UUID, int, UUID]] = field(default_factory=list)
    result_calls: list[tuple[UUID, UUID, ResultRequest, str]] = field(default_factory=list)
    clock_advance: float = 0.0
    clock: Any = None

    async def get_instruction(
        self, payout_id: UUID, attempt: int, company_id: UUID, *, request_id: str | None = None
    ) -> Instruction:
        self.instruction_calls.append((payout_id, attempt, company_id))
        if self.clock is not None and self.clock_advance:
            self.clock.advance(self.clock_advance)
        nxt = self.instructions.pop(0) if self.instructions else instruction()
        if isinstance(nxt, WorkerError):
            raise nxt
        return nxt

    async def post_result(
        self,
        payout_id: UUID,
        company_id: UUID,
        body: ResultRequest,
        idempotency_key: str,
        *,
        request_id: str | None = None,
    ) -> ResultResponse:
        self.result_calls.append((payout_id, company_id, body, idempotency_key))
        nxt = self.results.pop(0) if self.results else body.outcome
        if isinstance(nxt, WorkerError):
            raise nxt
        if isinstance(nxt, str):
            stored = "needs_operator" if nxt == "poison" else nxt
            return ResultResponse.model_validate(
                {"payout_id": payout_id, "attempt": body.attempt, "outcome": stored}
            )
        return nxt


@dataclass
class FakeBank:
    creates: list[BankCallResult] = field(default_factory=list)
    lookups: list[BankCallResult] = field(default_factory=list)
    create_calls: list[tuple[str, TransferRequest]] = field(default_factory=list)
    get_calls: list[str] = field(default_factory=list)

    async def create_transfer(self, key: str, body: TransferRequest) -> BankCallResult:
        self.create_calls.append((key, body))
        return self.creates.pop(0) if self.creates else bank_response()

    async def get_transfer(self, key: str) -> BankCallResult:
        self.get_calls.append(key)
        return self.lookups.pop(0) if self.lookups else bank_response(404, NOT_FOUND_BODY)


@dataclass
class FakeRepo:
    processed: set[UUID] = field(default_factory=set)
    calls: list[tuple[str, Any]] = field(default_factory=list)
    fail: WorkerError | None = None
    stored: StoredAnswer | None = None

    async def is_processed(self, event_id: UUID) -> bool:
        self.calls.append(("is_processed", event_id))
        if self.fail is not None:
            raise self.fail
        return event_id in self.processed

    async def upsert_submitting(self, env: Envelope) -> None:
        self.calls.append(("upsert_submitting", env.event_id))

    async def record_answer(
        self,
        payout_id: UUID,
        attempt: int,
        outcome: str,
        reason: str | None,
        bank_reference: str | None,
    ) -> None:
        self.calls.append(("record_answer", (outcome, reason, bank_reference)))

    async def finish(
        self,
        env: Envelope,
        outcome: str,
        reason: str | None = None,
        bank_reference: str | None = None,
    ) -> None:
        self.calls.append(("finish", (outcome, reason, bank_reference)))
        self.processed.add(env.event_id)

    async def definite_answer(self, payout_id: UUID, attempt: int) -> StoredAnswer | None:
        self.calls.append(("definite_answer", (payout_id, attempt)))
        return self.stored

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def last(self, name: str) -> Any:
        return [c[1] for c in self.calls if c[0] == name][-1]


def make_metrics(settings: Settings | None = None) -> Metrics:
    s = settings or make_settings()
    return Metrics.create(
        stream=s.payout_stream, group=s.payout_group, dlq_stream=s.payout_dlq_stream
    )


def requested_at(age: timedelta) -> datetime:
    return FAKE_EPOCH - age

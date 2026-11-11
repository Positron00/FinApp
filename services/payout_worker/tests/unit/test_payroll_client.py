"""``PayrollClient`` over a mock transport (design §3.4; PAY-405, PAY-411, R2, R10, C26, C30,
C34)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx
import pytest
from payout_worker.clients.payroll import PayrollClient
from payout_worker.errors import (
    AttemptNotCurrent,
    PayoutNotFound,
    PayrollRejected,
    PayrollUnavailable,
)
from payout_worker.jwt_signer import WorkerJwtSigner
from payout_worker.models import ResultRequest
from pycommon.auth import TokenVerifier
from pycommon.http import BreakerState
from pycommon.testing import FakeClock

from tests.helpers import (
    BANK_REF,
    COMPANY_ID,
    EVENT_ID,
    PAYOUT_ID,
    TOKENS,
    make_settings,
)

INSTRUCTION = {
    "payout_id": str(PAYOUT_ID),
    "attempt": 1,
    "amount_cents": 412_345,
    "currency": "USD",
    "routing": "021000021",
    "account": "000123456789",
    "requested_at": "2026-11-10T11:59:00Z",
}


def err(code: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": "m", "request_id": "r"}}


Handler = Callable[[httpx.Request], httpx.Response]


def client(
    handler: Handler, **settings: Any
) -> tuple[PayrollClient, list[httpx.Request], FakeClock]:
    seen: list[httpx.Request] = []
    clock = FakeClock()

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    s = make_settings(**settings)
    signer = WorkerJwtSigner(s.payout_worker_jwt_private_key, s.payout_worker_jwt_kid, clock=clock)
    return PayrollClient(s, signer, transport=httpx.MockTransport(record), clock=clock), seen, clock


def verifier(clock: FakeClock) -> TokenVerifier:
    return TokenVerifier(TOKENS.keyset, audience="payroll", clock=clock)


async def test_get_instruction_request_shape() -> None:
    """PAY-411: URL, attempt query, Bearer worker token with cid from the event, X-Request-Id."""
    c, seen, clock = client(lambda r: httpx.Response(200, json=INSTRUCTION))
    ins = await c.get_instruction(PAYOUT_ID, 1, COMPANY_ID, request_id=str(EVENT_ID))
    assert ins.amount_cents == 412_345
    assert ins.routing.get_secret_value() == "021000021"
    [req] = seen
    assert req.url.path == f"/v1/internal/payouts/{PAYOUT_ID}/instruction"
    assert req.url.params["attempt"] == "1"
    assert req.headers["X-Request-Id"] == str(EVENT_ID)
    assert req.headers["Accept"] == "application/json"
    token = req.headers["Authorization"].removeprefix("Bearer ")
    p = verifier(clock).verify(token)
    assert (p.iss, p.sub, p.cid, p.roles) == (
        "payout-worker",
        "service:payout-worker",
        COMPANY_ID,
        frozenset(),
    )
    await c.aclose()


async def test_post_result_shape_and_any_stored_outcome() -> None:
    """PAY-405, C26: body, Idempotency-Key <event_id>:<outcome>, 200 with outcome=requested."""
    out = {"payout_id": str(PAYOUT_ID), "attempt": 1, "outcome": "requested", "applied": False}
    c, seen, _ = client(lambda r: httpx.Response(200, json=out))
    body = ResultRequest(attempt=1, outcome="settled", bank_reference=BANK_REF)
    res = await c.post_result(PAYOUT_ID, COMPANY_ID, body, f"{EVENT_ID}:settled")
    assert res.outcome == "requested"
    [req] = seen
    assert req.method == "POST"
    assert req.url.path == f"/v1/internal/payouts/{PAYOUT_ID}/result"
    assert req.headers["Idempotency-Key"] == f"{EVENT_ID}:settled"
    assert UUID(str(res.payout_id)) == PAYOUT_ID
    import json  # noqa: PLC0415

    assert json.loads(req.content) == {
        "attempt": 1,
        "outcome": "settled",
        "reason": None,
        "bank_reference": BANK_REF,
    }


async def test_fresh_token_per_try_and_same_idempotency_key() -> None:
    """PAY-600, R10: retries on 503 mint a fresh JWT each try; the Idempotency-Key repeats."""
    answers = iter(
        [httpx.Response(503), httpx.Response(502), httpx.Response(200, json=INSTRUCTION)]
    )
    c, seen, _ = client(lambda r: next(answers))
    await c.get_instruction(PAYOUT_ID, 1, COMPANY_ID)
    tokens = [r.headers["Authorization"] for r in seen]
    assert len(tokens) == 3
    assert len(set(tokens)) == 3  # unique jti per try
    answers2 = iter(
        [
            httpx.Response(504),
            httpx.Response(
                200, json={"payout_id": str(PAYOUT_ID), "attempt": 1, "outcome": "settled"}
            ),
        ]
    )
    c2, seen2, _ = client(lambda r: next(answers2))
    await c2.post_result(
        PAYOUT_ID, COMPANY_ID, ResultRequest(attempt=1, outcome="indeterminate"), "k:indeterminate"
    )
    assert [r.headers["Idempotency-Key"] for r in seen2] == ["k:indeterminate"] * 2


@pytest.mark.parametrize("exc", [httpx.ConnectError("x"), httpx.ReadTimeout("x")])
async def test_retries_connect_errors_and_read_timeouts(exc: Exception) -> None:
    """R10: connect errors and read timeouts are retried (both calls are idempotent)."""
    calls = {"n": 0}

    def handler(r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc
        return httpx.Response(200, json=INSTRUCTION)

    c, _, _ = client(handler)
    await c.get_instruction(PAYOUT_ID, 1, COMPANY_ID)
    assert calls["n"] == 2


@pytest.mark.parametrize("status", [400, 401, 403, 409, 422, 500])
async def test_no_retry_on_other_statuses(status: int) -> None:
    """R10: only 429/502/503/504, connect errors and read timeouts are retried."""
    c, seen, _ = client(lambda r: httpx.Response(status, json=err("x")))
    with pytest.raises((PayrollRejected, PayrollUnavailable)):
        await c.get_instruction(PAYOUT_ID, 1, COMPANY_ID)
    assert len(seen) == 1


async def test_retries_exhausted_is_unavailable() -> None:
    """PAY-411: 1 try + 3 retries on 503, then PayrollUnavailable."""
    c, seen, _ = client(lambda r: httpx.Response(503))
    with pytest.raises(PayrollUnavailable) as e:
        await c.get_instruction(PAYOUT_ID, 1, COMPANY_ID)
    assert e.value.code == "payroll_unavailable"
    assert len(seen) == 4


@pytest.mark.parametrize(
    ("status", "body", "exc", "code"),
    [
        (409, err("attempt_not_current"), AttemptNotCurrent, "attempt_not_current"),
        (404, err("payout_not_found"), PayoutNotFound, "payout_not_found"),
        (400, err("idempotency_key_required"), PayrollRejected, "idempotency_key_required"),
        (401, err("invalid_token"), PayrollRejected, "invalid_token"),
        (403, err("forbidden_role"), PayrollRejected, "forbidden_role"),
        (409, err("conflict"), PayrollRejected, "conflict"),
        (422, err("validation_error"), PayrollRejected, "validation_error"),
        (418, {"nope": 1}, PayrollRejected, "http_418"),
        (418, err("Bad Code"), PayrollRejected, "http_418"),
        (400, None, PayrollRejected, "http_400"),
        (429, err("rate_limited"), PayrollUnavailable, "payroll_unavailable"),
        (500, err("internal_error"), PayrollUnavailable, "payroll_unavailable"),
        (200, {"payout_id": "x"}, PayrollRejected, "malformed_response"),
    ],
)
async def test_instruction_mapping(status: int, body: Any, exc: type[Exception], code: str) -> None:
    """Design §3.4: the response mapping table for the instruction."""
    resp = (
        (lambda r: httpx.Response(status, content=b"not json"))
        if body is None
        else (lambda r: httpx.Response(status, json=body))
    )
    c, _, _ = client(resp, payout_payroll_max_retries=0)
    with pytest.raises(exc) as e:
        await c.get_instruction(PAYOUT_ID, 1, COMPANY_ID)
    assert e.value.code == code  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("status", "body", "exc", "code"),
    [
        (409, err("attempt_not_current"), AttemptNotCurrent, "attempt_not_current"),
        (404, err("payout_not_found"), PayrollRejected, "payout_not_found"),
        (422, err("idempotency_key_reused"), PayrollRejected, "idempotency_key_reused"),
        (503, err("db_timeout"), PayrollUnavailable, "payroll_unavailable"),
        (200, {"outcome": "weird"}, PayrollRejected, "malformed_response"),
    ],
)
async def test_result_mapping(status: int, body: Any, exc: type[Exception], code: str) -> None:
    """Design §3.4: the response mapping table for the result (404 on the result is rejected)."""
    c, _, _ = client(lambda r: httpx.Response(status, json=body), payout_payroll_max_retries=0)
    with pytest.raises(exc) as e:
        await c.post_result(
            PAYOUT_ID, COMPANY_ID, ResultRequest(attempt=1, outcome="indeterminate"), "k:i"
        )
    assert e.value.code == code  # type: ignore[attr-defined]


async def test_breaker_opens_after_five_failures_not_on_429() -> None:
    """Plan §9: the payroll breaker opens after 5 consecutive failures (429 doesn't count);
    while open, calls fail at once with breaker_open; it half-opens after 30 s."""
    c, _, _ = client(lambda r: httpx.Response(429), payout_payroll_max_retries=0)
    for _ in range(6):
        with pytest.raises(PayrollUnavailable):
            await c.get_instruction(PAYOUT_ID, 1, COMPANY_ID)
    assert c.http.breaker.state is BreakerState.closed
    c2, seen2, clock2 = client(lambda r: httpx.Response(500), payout_payroll_max_retries=0)
    for _ in range(5):
        with pytest.raises(PayrollUnavailable):
            await c2.get_instruction(PAYOUT_ID, 1, COMPANY_ID)
    with pytest.raises(PayrollUnavailable) as e:
        await c2.get_instruction(PAYOUT_ID, 1, COMPANY_ID)
    assert e.value.code == "breaker_open"
    assert len(seen2) == 5
    clock2.advance(30)
    assert c2.http.breaker.state is BreakerState.half_open


async def test_deadline_is_unavailable() -> None:
    """R2: the total deadline PAYOUT_PAYROLL_CALL_DEADLINE_S bounds all tries."""

    def slow(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    c, seen, _ = client(slow, payout_payroll_call_deadline_s=0.5)
    with pytest.raises(PayrollUnavailable):
        await c.get_instruction(PAYOUT_ID, 1, COMPANY_ID)
    assert 1 <= len(seen) <= 4

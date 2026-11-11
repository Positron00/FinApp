"""``BankClient`` over a mock transport (design §3.5; PAY-404, PAY-412, ADR 0003 a and e)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from payout_worker.classify import classify_transfer_response
from payout_worker.clients.bank import BankClient
from payout_worker.models import NoAnswer, Rejected, TransferRequest
from pycommon.http import BreakerState
from pycommon.testing import FakeClock

from tests.helpers import ACCOUNT, API_KEY, KEY, ROUTING, make_settings, transfer_json

BODY = TransferRequest(amount_cents=412_345, currency="USD", routing=ROUTING, account=ACCOUNT)
Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler, **settings: Any) -> tuple[BankClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return (
        BankClient(
            make_settings(**settings), transport=httpx.MockTransport(record), clock=FakeClock()
        ),
        seen,
    )


async def test_create_transfer_headers_and_body() -> None:
    """PAY-412: POST /v1/transfers with Idempotency-Key <payout_id>:<attempt>, X-Api-Key, body."""
    c, seen = client(lambda r: httpx.Response(201, json=transfer_json()))
    result = await c.create_transfer(KEY, BODY)
    assert result.response is not None
    assert result.response.status_code == 201
    [req] = seen
    assert req.url.path == "/v1/transfers"
    assert req.headers["Idempotency-Key"] == KEY
    assert req.headers["X-Api-Key"] == API_KEY
    assert req.headers["User-Agent"].startswith("payrun-payout-worker/")
    assert json.loads(req.content) == {
        "amount_cents": 412_345,
        "currency": "USD",
        "routing": ROUTING,
        "account": ACCOUNT,
    }
    await c.aclose()


async def test_get_transfer_path_encoded() -> None:
    """PAY-410: GET /v1/transfers/{key} with the key path-encoded; no Idempotency-Key."""
    c, seen = client(lambda r: httpx.Response(404, json={"error": {}}))
    result = await c.get_transfer(KEY)
    assert result.response is not None
    assert result.response.status_code == 404
    assert seen[0].url.raw_path.decode().endswith(KEY.replace(":", "%3A"))
    assert "Idempotency-Key" not in seen[0].headers


@pytest.mark.parametrize("status", [429, 502, 503, 504])
async def test_retries_same_key(status: int) -> None:
    """PAY-404: 429/502/503/504 are retried with the same Idempotency-Key."""
    answers = iter([httpx.Response(status), httpx.Response(200, json=transfer_json())])
    c, seen = client(lambda r: next(answers))
    await c.create_transfer(KEY, BODY)
    assert [r.headers["Idempotency-Key"] for r in seen] == [KEY, KEY]


@pytest.mark.parametrize("exc", [httpx.ConnectError("x"), httpx.ReadTimeout("x")])
async def test_retries_transport_errors(exc: Exception) -> None:
    """PAY-404: connect errors and timeouts are retried with the same key."""
    calls = {"n": 0}

    def handler(r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise exc
        return httpx.Response(201, json=transfer_json())

    c, _ = client(handler)
    result = await c.create_transfer(KEY, BODY)
    assert calls["n"] == 3
    assert result.error is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 500])
async def test_no_retry_and_returned_not_raised(status: int) -> None:
    """ADR 0003 e: 500 and 4xx aren't retried within the call; they're returned, not raised."""
    c, seen = client(lambda r: httpx.Response(status, json=transfer_json("rejected")))
    result = await c.create_transfer(KEY, BODY)
    assert len(seen) == 1
    assert result.response is not None
    assert result.response.status_code == status
    assert isinstance(classify_transfer_response(result), NoAnswer)


async def test_exhausted_transport_errors_are_returned() -> None:
    """PAY-404: after the retries a timeout is returned as an error, never raised."""

    def handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    c, seen = client(handler)
    result = await c.create_transfer(KEY, BODY)
    assert result.response is None
    assert classify_transfer_response(result) == NoAnswer("connect_error")
    assert len(seen) == 4
    g = await c.get_transfer(KEY)
    assert g.error is not None


async def test_breaker_counting() -> None:
    """Plan §9: 2xx rejected, 429 and 4xx don't count; 5 server errors open the breaker, then
    calls fail at once with breaker_open."""
    rejected, _ = client(lambda r: httpx.Response(200, json=transfer_json("rejected")))
    for _ in range(6):
        assert isinstance(
            classify_transfer_response(await rejected.create_transfer(KEY, BODY)), Rejected
        )
    assert rejected.http.breaker.state is BreakerState.closed
    four, _ = client(lambda r: httpx.Response(422), bank_max_retries=0)
    for _ in range(6):
        await four.create_transfer(KEY, BODY)
    assert four.http.breaker.state is BreakerState.closed
    down, seen_down = client(lambda r: httpx.Response(500), bank_max_retries=0)
    for _ in range(5):
        await down.create_transfer(KEY, BODY)
    assert down.http.breaker.state is BreakerState.open
    result = await down.create_transfer(KEY, BODY)
    assert classify_transfer_response(result) == NoAnswer("breaker_open")
    assert len(seen_down) == 5


async def test_total_deadline() -> None:
    """PAY-404: BANK_TOTAL_DEADLINE_S bounds all tries; the result is a timeout, not raised."""

    def handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    c, _ = client(handler, bank_total_deadline_s=0.3)
    result = await c.create_transfer(KEY, BODY)
    assert result.error is not None

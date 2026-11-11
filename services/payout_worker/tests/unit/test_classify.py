"""Bank answer classification, one case per row (design §3.5.3; PAY-404, ADR 0003 e)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from payout_worker.classify import (
    BankCallResult,
    classify_lookup_response,
    classify_transfer_response,
)
from payout_worker.models import NoAnswer, NotFound, Rejected, Settled
from pycommon.http import ProviderTimeout, ProviderUnavailable

from tests.helpers import BANK_REF, NOT_FOUND_BODY, bank_response, transfer_json


def test_201_settled_and_200_replay_settled() -> None:
    """PAY-404: 2xx settled with a reference → Settled (201 new, 200 replay; R6)."""
    for code in (201, 200):
        answer = classify_transfer_response(bank_response(code))
        assert isinstance(answer, Settled)
        assert answer.bank_reference == BANK_REF


def test_200_rejected_with_reason() -> None:
    """PAY-404: 2xx rejected with a reason is the only definite rejection."""
    answer = classify_transfer_response(bank_response(200, transfer_json("rejected")))
    assert answer == Rejected(answer.transfer_id, "account_closed")  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "body",
    [
        transfer_json("rejected", reason=None),
        transfer_json("rejected", reason="Bad Reason!"),
        transfer_json("settled", bank_reference=None),
        transfer_json("settled", bank_reference="x" * 65),
        transfer_json("settled", reason="account_closed"),
        transfer_json("rejected", bank_reference=BANK_REF),
        transfer_json("pending"),
        {"status": "settled"},
    ],
)
def test_malformed_2xx_bodies_are_no_answer(body: dict[str, Any]) -> None:
    """PAY-404: anything else in a 2xx is NoAnswer("malformed_body"), never Rejected."""
    assert classify_transfer_response(bank_response(201, body)) == NoAnswer("malformed_body", 201)


def test_invalid_json_is_no_answer() -> None:
    """PAY-404: a non-JSON 2xx body → malformed_body."""
    assert classify_transfer_response(bank_response(200, raw=b"<html>")) == NoAnswer(
        "malformed_body", 200
    )


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (101, "http_other"),
        (302, "http_other"),
        (400, "http_4xx_other"),
        (401, "http_401"),
        (403, "http_403"),
        (404, "http_404"),
        (409, "http_409"),
        (410, "http_4xx_other"),
        (422, "http_422"),
        (429, "http_429"),
        (500, "http_5xx"),
        (502, "http_5xx"),
        (503, "http_5xx"),
        (504, "http_5xx"),
    ],
)
def test_non_2xx_is_never_rejected(status: int, reason: str) -> None:
    """PAY-404, ADR 0003 e: every non-2xx status is NoAnswer with its reason."""
    rejected_body = transfer_json("rejected")
    r = BankCallResult(httpx.Response(status, json=rejected_body))
    assert classify_transfer_response(r) == NoAnswer(reason, status)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ProviderTimeout("bank", "create_transfer"), "timeout"),
        (ProviderUnavailable("bank", "create_transfer", "connect_error"), "connect_error"),
        (ProviderUnavailable("bank", "create_transfer", "breaker_open"), "breaker_open"),
    ],
)
def test_transport_errors(error: Any, reason: Any) -> None:
    """PAY-404: timeout, connect error and an open breaker are no answer."""
    assert classify_transfer_response(BankCallResult(None, error)) == NoAnswer(reason)
    assert classify_lookup_response(BankCallResult(None, error)) == NoAnswer(reason)


def test_missing_response_without_error() -> None:
    """PAY-404: a result with neither response nor error is a connect error."""
    assert classify_transfer_response(BankCallResult(None)) == NoAnswer("connect_error")


def test_lookup() -> None:
    """PAY-410: GET 404 transfer_not_found → NotFound; 200 settled/rejected as for POST; 500 no
    answer."""
    assert classify_lookup_response(bank_response(404, NOT_FOUND_BODY)) == NotFound()
    assert isinstance(classify_lookup_response(bank_response(200)), Settled)
    assert isinstance(
        classify_lookup_response(bank_response(200, transfer_json("rejected"))), Rejected
    )
    assert classify_lookup_response(bank_response(500, {})) == NoAnswer("http_5xx", 500)


@pytest.mark.parametrize(
    "resp",
    [
        bank_response(404, {"error": {}}),
        bank_response(404, {"error": {"code": "not_found"}}),
        bank_response(404, {"detail": "Not Found"}),
        bank_response(404, ["transfer_not_found"]),
        bank_response(404, raw=b"<html>404</html>"),
    ],
)
def test_lookup_other_404_is_no_answer(resp: BankCallResult) -> None:
    """C63, PAY-410: only the bank's transfer_not_found is NotFound; a misrouted 404 (wrong base
    URL, a proxy page) is no answer, so it never becomes poison key_window_expired."""
    assert classify_lookup_response(resp) == NoAnswer("http_404", 404)

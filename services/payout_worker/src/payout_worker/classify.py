"""Bank answer classification (design §3.5.3; PAY-404, ADR 0003 e). Pure functions.

Only a 2xx with ``status: rejected`` and a valid ``reason`` is a definite rejection. Everything
else that isn't a settled 2xx is "no answer". Nothing here can return ``Rejected`` for a non-2xx.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

import httpx
from pycommon.http import ProviderError, ProviderTimeout, ProviderUnavailable
from pydantic import ValidationError

from payout_worker.models import (
    NoAnswer,
    NoAnswerReason,
    NotFound,
    Rejected,
    Settled,
    TransferResponse,
)

__all__ = ["BankCallResult", "classify_lookup_response", "classify_transfer_response"]

NOT_FOUND_CODE: Final = "transfer_not_found"


@dataclass(frozen=True)
class BankCallResult:
    response: httpx.Response | None  # the last response, if any
    error: ProviderError | None = None  # ProviderTimeout | ProviderUnavailable


def _error_reason(err: ProviderError) -> NoAnswerReason:
    if isinstance(err, ProviderTimeout):
        return "timeout"
    if isinstance(err, ProviderUnavailable) and err.reason == "breaker_open":
        return "breaker_open"
    return "connect_error"


def _status_reason(status: int) -> NoAnswerReason:
    if status == 429:
        return "http_429"
    if status >= 500:
        return "http_5xx"
    named: dict[int, NoAnswerReason] = {
        401: "http_401",
        403: "http_403",
        404: "http_404",
        409: "http_409",
        422: "http_422",
    }
    if status in named:
        return named[status]
    if 400 <= status < 500:
        return "http_4xx_other"
    return "http_other"


def _classify_2xx(resp: httpx.Response) -> Settled | Rejected | NoAnswer:
    try:
        body = TransferResponse.model_validate_json(resp.content)
    except ValidationError:
        return NoAnswer("malformed_body", resp.status_code)
    if body.status == "settled" and body.bank_reference is not None and body.reason is None:
        return Settled(body.transfer_id, body.bank_reference)
    if body.status == "rejected" and body.reason is not None and body.bank_reference is None:
        return Rejected(body.transfer_id, body.reason)
    return NoAnswer("malformed_body", resp.status_code)


def classify_transfer_response(r: BankCallResult) -> Settled | Rejected | NoAnswer:
    if r.error is not None or r.response is None:
        return NoAnswer(_error_reason(r.error) if r.error is not None else "connect_error")
    status = r.response.status_code
    if 200 <= status < 300:
        return _classify_2xx(r.response)
    return NoAnswer(_status_reason(status), status)


def _is_transfer_not_found(resp: httpx.Response) -> bool:
    try:
        body = json.loads(resp.content)
    except ValueError:
        return False
    err = body.get("error") if isinstance(body, dict) else None
    return isinstance(err, dict) and err.get("code") == NOT_FOUND_CODE


def classify_lookup_response(r: BankCallResult) -> Settled | Rejected | NotFound | NoAnswer:
    """As ``classify_transfer_response``, plus: a 404 whose body has the bank's
    ``transfer_not_found`` code is ``NotFound``. Any other 404 (a wrong base URL, a proxy page)
    is ``NoAnswer("http_404")``, never "no transfer" (C63)."""
    if (
        r.response is not None
        and r.error is None
        and r.response.status_code == 404
        and _is_transfer_not_found(r.response)
    ):
        return NotFound()
    return classify_transfer_response(r)

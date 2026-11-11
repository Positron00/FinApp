"""API models (design §2.3; PAY-412, PAY-NFR-07, R6)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from bank_stub.schemas import IDEMPOTENCY_KEY_RE, TransferRequest, TransferResponse
from pydantic import ValidationError

from tests.helpers import ACCOUNT, ROUTING, body


@pytest.mark.parametrize(
    "change",
    [
        {"amount_cents": 0},
        {"amount_cents": 1_000_000_001},
        {"currency": "EUR"},
        {"routing": "12345678"},
        {"routing": "12345678a"},
        {"account": "123"},
        {"account": "1" * 18},
        {"extra": 1},
    ],
)
def test_transfer_request_rejects(change: dict[str, Any]) -> None:
    """PAY-412: bounds, patterns and extra="forbid"."""
    with pytest.raises(ValidationError):
        TransferRequest.model_validate({**body(), **change})


def test_transfer_request_accepts_bounds() -> None:
    """PAY-412: 1 cent to $10M; 4 to 17 account digits."""
    TransferRequest.model_validate(body(amount=1, account="1234"))
    TransferRequest.model_validate(body(amount=1_000_000_000, account="1" * 17))


def test_repr_masks_routing_and_account() -> None:
    """PAY-NFR-07: repr never shows routing or account."""
    text = repr(TransferRequest.model_validate(body()))
    assert ROUTING not in text
    assert ACCOUNT not in text
    assert "***" in text


def test_key_pattern() -> None:
    """R6: <uuid>:<n> is accepted; spaces, slashes and >255 characters are not."""
    assert IDEMPOTENCY_KEY_RE.match(f"{uuid4()}:3")
    assert IDEMPOTENCY_KEY_RE.match("a" * 255)
    for bad in ("", "a b", "a/b", "a" * 256, "ü"):
        assert not IDEMPOTENCY_KEY_RE.match(bad)


def test_response_has_exactly_four_fields() -> None:
    """R6, PAY-412: {transfer_id, status, reason, bank_reference}."""
    r = TransferResponse(
        transfer_id=uuid4(), status="settled", reason=None, bank_reference="BSR-0123456789ABCDEF"
    )
    assert set(r.model_dump(mode="json")) == {"transfer_id", "status", "reason", "bank_reference"}
    with pytest.raises(ValidationError):
        TransferResponse(
            transfer_id=uuid4(), status="settled", reason=None, bank_reference="x" * 65
        )

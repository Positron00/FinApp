"""Wire models and envelope parsing (design §2.3; PAY-402, PAY-NFR-07, R7, C22, C26)."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from payout_worker.errors import InvalidMessage
from payout_worker.models import (
    Instruction,
    ResultRequest,
    ResultResponse,
    StreamMessage,
    TransferRequest,
    parse_envelope,
)
from pydantic import ValidationError

from tests.helpers import (
    ACCOUNT,
    BANK_REF,
    COMPANY_ID,
    EVENT_ID,
    KEY,
    PAYOUT_ID,
    ROUTING,
    TRACEPARENT,
    envelope_dict,
    instruction,
    message,
)


def test_parses_the_three_r7_fields() -> None:
    """PAY-402, R7: the envelope field is authoritative; IDs and the bank key come from it."""
    env = parse_envelope(message())
    assert env.event_id == EVENT_ID
    assert env.data.payout_id == PAYOUT_ID
    assert env.traceparent == TRACEPARENT
    assert env.bank_key == KEY


def test_str_keys_are_accepted() -> None:
    """R7: decoded (str) field names work too."""
    doc = envelope_dict()
    msg = StreamMessage("1-0", {"event_id": str(EVENT_ID), "envelope": json.dumps(doc)})
    assert parse_envelope(msg).event_id == EVENT_ID


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=1),
        lambda d: d["data"].update(extra=1),
        lambda d: d["data"].update(attempt=0),
        lambda d: d["data"].update(amount_cents=0),
        lambda d: d["data"].update(amount_cents=1_000_000_001),
        lambda d: d["data"].update(currency="EUR"),
        lambda d: d.pop("traceparent"),
        lambda d: d.update(traceparent="00-abc"),
        lambda d: d.update(type="payout.other"),
        lambda d: d.update(version=2),
    ],
)
def test_invalid_envelopes_keep_readable_ids(mutate: Any) -> None:
    """PAY-402, C22: schema violations (traceparent required) raise InvalidMessage with the IDs."""
    doc = envelope_dict()
    mutate(doc)
    with pytest.raises(InvalidMessage) as e:
        parse_envelope(message(doc))
    assert e.value.payout_id == PAYOUT_ID
    assert e.value.company_id == COMPANY_ID
    assert e.value.event_id == EVENT_ID


def test_unreadable() -> None:
    """PAY-402: no envelope field, invalid JSON or a non-object → InvalidMessage without IDs."""
    for msg in (message(drop_envelope=True), message(raw=b"{"), message(raw=b"[1]")):
        with pytest.raises(InvalidMessage) as e:
            parse_envelope(msg)
        assert not e.value.ids_readable
    bad_attempt = envelope_dict(attempt=True)
    with pytest.raises(InvalidMessage) as e2:
        parse_envelope(message(bad_attempt))
    assert e2.value.attempt is None


def test_entry_event_id_is_the_fallback() -> None:
    """Design §2.3: the entry's event_id field is used only when the envelope can't be read."""
    msg = message(raw=b"nope", event_id_field="0193f1d2-6c4a-7b1e-9a3f-000000000077")
    with pytest.raises(InvalidMessage) as e:
        parse_envelope(msg)
    assert e.value.event_id == UUID("0193f1d2-6c4a-7b1e-9a3f-000000000077")


def test_secret_fields_never_reveal_numbers() -> None:
    """PAY-NFR-07: Instruction keeps routing/account as SecretStr; TransferRequest masks them."""
    ins = instruction()
    for text in (repr(ins), str(ins), ins.model_dump_json()):
        assert ROUTING not in text
        assert ACCOUNT not in text
    tr = TransferRequest(amount_cents=1, currency="USD", routing=ROUTING, account=ACCOUNT)
    assert ROUTING not in repr(tr)
    assert ACCOUNT not in repr(tr)


def test_instruction_ignores_extra() -> None:
    """Design §2.3: received models ignore additive fields."""
    assert isinstance(
        Instruction.model_validate(
            {**instruction().model_dump(), "new": 1, "routing": ROUTING, "account": ACCOUNT}
        ),
        Instruction,
    )


@pytest.mark.parametrize(
    "body",
    [
        {"attempt": 1, "outcome": "settled", "bank_reference": BANK_REF},
        {"attempt": 1, "outcome": "rejected", "reason": "account_closed"},
        {"attempt": 1, "outcome": "indeterminate"},
        {"attempt": 1, "outcome": "poison", "reason": "schema_invalid"},
        {"attempt": 1, "outcome": "poison", "reason": "key_window_expired"},
    ],
)
def test_result_request_valid(body: dict[str, Any]) -> None:
    """PAY-405: the per-outcome rules."""
    ResultRequest.model_validate(body)


@pytest.mark.parametrize(
    "body",
    [
        {"attempt": 1, "outcome": "settled"},
        {"attempt": 1, "outcome": "settled", "bank_reference": "x" * 65},
        {"attempt": 1, "outcome": "rejected"},
        {"attempt": 1, "outcome": "indeterminate", "reason": "x"},
        {"attempt": 1, "outcome": "poison", "reason": "other"},
        {"attempt": 1, "outcome": "failed"},
        {"attempt": 0, "outcome": "indeterminate"},
    ],
)
def test_result_request_invalid(body: dict[str, Any]) -> None:
    """PAY-405, C26: bank_reference ≤ 64; never failed; per-outcome fields."""
    with pytest.raises(ValidationError):
        ResultRequest.model_validate(body)


def test_result_response_any_status() -> None:
    """C26: the stored outcome takes any of the five attempt statuses; extras ignored."""
    for status in ("requested", "indeterminate", "needs_operator", "settled", "rejected"):
        r = ResultResponse.model_validate(
            {"payout_id": str(PAYOUT_ID), "attempt": 1, "outcome": status, "applied": True}
        )
        assert r.outcome == status

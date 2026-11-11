"""Opaque cursors (design §4.9; PAY-202, PAY-303, PAY-602, C14)."""

from __future__ import annotations

import base64
import json
from uuid import uuid4

import pytest
from payroll.api.pagination import decode_cursor, encode_cursor
from payroll.domain import InvalidCursor


def _b64(doc: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(doc).encode()).decode().rstrip("=")


def test_round_trip_and_opaque() -> None:
    """PAY-202: encode/decode round trip; base64url without padding; only an ID inside."""
    rid = uuid4()
    cursor = encode_cursor(rid)
    assert "=" not in cursor
    assert decode_cursor(cursor) == rid
    assert json.loads(base64.urlsafe_b64decode(cursor + "==")) == {"v": 1, "after": str(rid)}


@pytest.mark.parametrize(
    "cursor",
    ["", "!!!", "bm90IGpzb24", _b64([1]), _b64({"v": 2, "after": str(uuid4())}), _b64({"v": 1}),
     _b64({"v": 1, "after": "nope"})],
)  # fmt: skip
def test_bad_cursor_is_validation_error(cursor: str) -> None:
    """PAY-602: garbage, a wrong version or a non-UUID → validation_error, issue invalid_cursor."""
    with pytest.raises(InvalidCursor) as err:
        decode_cursor(cursor)
    assert err.value.status == 422
    assert err.value.error_code == "validation_error"
    assert err.value.details == [{"field": "query.cursor", "issue": "invalid_cursor"}]

"""Opaque keyset cursors (design §4.9; R4, C8, C14): ``base64url_nopad({"v": 1, "after": id})``."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Final
from uuid import UUID

from payroll.domain import InvalidCursor

__all__ = ["CURSOR_VERSION", "decode_cursor", "encode_cursor"]

CURSOR_VERSION: Final = 1


def encode_cursor(after_id: UUID) -> str:
    raw = json.dumps({"v": CURSOR_VERSION, "after": str(after_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> UUID:
    """The ``after`` ID; anything undecodable raises 422 ``invalid_cursor``."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        doc = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if not isinstance(doc, dict) or doc.get("v") != CURSOR_VERSION:
            raise InvalidCursor()
        return UUID(str(doc["after"]))
    except InvalidCursor:
        raise
    except binascii.Error, ValueError, KeyError, TypeError, UnicodeDecodeError:
        raise InvalidCursor() from None

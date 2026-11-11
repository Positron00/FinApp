"""Invite and reset tokens (identity LLD §6.7)."""

from __future__ import annotations

import base64
import hashlib

import pytest
from identity.domain.tokens import digest_of, new_token


def test_new_token_is_32_random_bytes_as_43_char_base64url() -> None:
    """PAY-101, PAY-105: 43-char unpadded base64url; digest = SHA-256 of the raw bytes."""
    t = new_token()
    assert len(t.raw) == 43
    assert "=" not in t.raw
    raw_bytes = base64.urlsafe_b64decode(t.raw + "=")
    assert len(raw_bytes) == 32
    assert t.digest == hashlib.sha256(raw_bytes).digest()
    assert digest_of(t.raw) == t.digest
    assert new_token().raw != t.raw
    assert t.raw not in repr(t)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "a" * 42,
        "a" * 44,
        "a" * 42 + "=",
        "a" * 42 + "+",
        "a" * 42 + "/",
        "a" * 42 + " ",
        "A" * 42 + "B",  # non-canonical: stray low bits in the last character
    ],
)
def test_digest_of_rejects_malformed(raw: str) -> None:
    """PAY-103: wrong length, padding, alphabet or a non-canonical spelling -> None."""
    assert digest_of(raw) is None

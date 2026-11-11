"""IP normalisation and keyed hashes (identity LLD §6.8)."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from identity.domain.ip import email_key, ip_hash, normalise_ip


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.7", "203.0.113.7"),
        (" 203.0.113.7 ", "203.0.113.7"),
        ("2001:DB8:0:0:0:0:0:1", "2001:db8::1"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
    ],
)
def test_normalise(raw: str, expected: str) -> None:
    """PAY-NFR-08: IPv4/IPv6 normalisation; IPv4-mapped IPv6 becomes IPv4."""
    assert normalise_ip(raw) == expected


@pytest.mark.parametrize("raw", ["", "300.1.1.1", "not-an-ip", "1.2.3"])
def test_normalise_rejects_invalid(raw: str) -> None:
    """PAY-NFR-08: an invalid address raises ValueError (422 at the API)."""
    with pytest.raises(ValueError, match="does not appear"):
        normalise_ip(raw)


def test_ip_hash_is_hmac_sha256() -> None:
    """PAY-NFR-08: ip_hash = HMAC-SHA256(AUDIT_IP_HASH_KEY, ip), 64 hex chars."""
    key = b"key"
    expected = hmac.new(key, b"203.0.113.7", hashlib.sha256).hexdigest()
    assert ip_hash("::ffff:203.0.113.7", key) == expected
    assert len(expected) == 64
    assert ip_hash("203.0.113.7", b"other") != expected


def test_email_key_is_case_insensitive_and_keyed() -> None:
    """PAY-104, PAY-NFR-08: the account subject is an HMAC of the lower-cased email."""
    assert email_key("Ann@Example.com", b"k") == email_key("ann@example.com ", b"k")
    assert email_key("ann@example.com", b"k") != email_key("ann@example.com", b"j")
    assert "ann" not in email_key("ann@example.com", b"k")

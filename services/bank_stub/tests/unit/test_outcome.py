"""The outcome function and bank reference (design §3.4; PAY-412)."""

from __future__ import annotations

import re
import secrets

from bank_stub.domain.outcome import bank_reference_for, bucket_of, decide_outcome


def _key_with_bucket(target: int) -> str:
    n = 0
    while True:
        key = f"k-{n}"
        if bucket_of(key) == target:
            return key
        n += 1


def test_account_ending_0000_is_closed_even_at_rate_0() -> None:
    """PAY-412: an account ending 0000 is rejected / account_closed, checked first."""
    assert decide_outcome("any", "123450000", 0.0) == ("rejected", "account_closed")
    assert decide_outcome("any", "0000", 1.0) == ("rejected", "account_closed")


def test_same_key_same_outcome() -> None:
    """PAY-412: the outcome is deterministic per key."""
    for _ in range(50):
        key = secrets.token_hex(8)
        assert decide_outcome(key, "12345678", 0.5) == decide_outcome(key, "99999999", 0.5)


def test_rate_bounds() -> None:
    """PAY-412: rate 0 never rejects; rate 1 always does."""
    keys = [secrets.token_hex(8) for _ in range(500)]
    assert all(decide_outcome(k, "12345678", 0.0) == ("settled", None) for k in keys)
    assert all(
        decide_outcome(k, "12345678", 1.0) == ("rejected", "insufficient_funds_at_bank")
        for k in keys
    )


def test_threshold_boundary() -> None:
    """PAY-412: at 0.05 bucket 499 is rejected and bucket 500 isn't."""
    assert decide_outcome(_key_with_bucket(499), "12345678", 0.05)[0] == "rejected"
    assert decide_outcome(_key_with_bucket(500), "12345678", 0.05) == ("settled", None)


def test_rejected_share_is_about_five_percent() -> None:
    """PAY-412: over 10 000 random keys at 0.05 the rejected share is 5% ± 1%."""
    rejected = sum(
        decide_outcome(secrets.token_hex(12), "12345678", 0.05)[0] == "rejected"
        for _ in range(10_000)
    )
    assert 400 <= rejected <= 600


def test_bank_reference_format() -> None:
    """PAY-412, C26: BSR- plus 16 uppercase hex, deterministic, at most 64 characters."""
    ref = bank_reference_for("p:1")
    assert re.fullmatch(r"^BSR-[0-9A-F]{16}$", ref)
    assert len(ref) <= 64
    assert ref == bank_reference_for("p:1") != bank_reference_for("p:2")

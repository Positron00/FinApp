"""Email backoff (identity LLD §6.9)."""

from __future__ import annotations

from identity.domain.email_schedule import is_exhausted, retry_delay_s


def test_backoff_sequence_without_jitter() -> None:
    """PAY-110: 30, 60 … 1800 s, capped."""
    seq = [retry_delay_s(n, 30, 1800, jitter=0) for n in range(1, 10)]
    assert seq == [30, 60, 120, 240, 480, 960, 1800, 1800, 1800]
    assert retry_delay_s(0, 30, 1800, jitter=0) == 30


def test_backoff_jitter_stays_within_ten_percent() -> None:
    """PAY-110: ±10% jitter."""
    for _ in range(200):
        assert 27 <= retry_delay_s(1, 30, 1800) <= 33


def test_exhaustion_at_max_attempts() -> None:
    """PAY-110: the 10th failure marks the email failed."""
    assert not is_exhausted(9, 10)
    assert is_exhausted(10, 10)

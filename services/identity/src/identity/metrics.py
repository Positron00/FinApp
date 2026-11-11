"""Identity instruments (identity LLD §5.2; PAY-104, PAY-110, PAY-603, PAY-NFR-10).

Created at app start-up (not import time), so they bind to the meter provider that is current
then. The email gauges and their refresher arrive with F1.
"""

from __future__ import annotations

from dataclasses import dataclass

from opentelemetry.metrics import Counter, Histogram
from pycommon.otel import SECONDS_BUCKETS, histogram, meter

__all__ = ["IdentityMetrics", "create_metrics"]


@dataclass(frozen=True)
class IdentityMetrics:
    signins: Counter
    rate_limited: Counter
    limiter_errors: Counter
    emails_failed: Counter
    hash_seconds: Histogram


def create_metrics() -> IdentityMetrics:
    m = meter("identity")
    signins = m.create_counter(
        "auth_signins", unit="1", description="Login responses by outcome (C19)"
    )
    rate_limited = m.create_counter(
        "rate_limited", unit="1", description="Requests denied by a rate limit"
    )
    limiter_errors = m.create_counter(
        "rate_limiter_errors", unit="1", description="Redis failures in the rate limiter"
    )
    emails_failed = m.create_counter(
        "identity_emails_failed", unit="1", description="Emails that reached status failed (R25)"
    )
    emails_failed.add(0)
    hash_seconds = histogram(
        "identity_password_hash_seconds",
        unit="s",
        description="argon2 hash and verify duration",
        buckets=SECONDS_BUCKETS,
    )
    return IdentityMetrics(signins, rate_limited, limiter_errors, emails_failed, hash_seconds)

"""Identity counters export 0 before their first event (C21, C72; PAY-104, PAY-NFR-10)."""

from __future__ import annotations

from identity.metrics import create_metrics
from pycommon.testing import OtelMemory


def test_signin_outcomes_and_email_failures_start_at_zero(otel_memory: OtelMemory) -> None:
    """PAY-104, PAY-NFR-10: SigninFailuresSpike and EmailOutboxStuck read increase() on these
    counters, so each series must exist at 0 before the first failure."""
    create_metrics()
    signins = {a["outcome"]: v for a, v in otel_memory.metric_points("auth_signins")}
    assert signins == {"success": 0, "failure": 0, "rate_limited": 0}
    assert [v for _, v in otel_memory.metric_points("identity_emails_failed")] == [0]

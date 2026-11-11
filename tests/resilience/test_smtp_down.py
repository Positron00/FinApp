"""SMTP down (HLD §4 row "SMTP down"; plan §12.1; PAY-110).

Mailpit is the dev SMTP server; stopping it takes SMTP away from identity's email relay. Expected:
the invite still answers 201, the email stays queued (``pending``, its first attempt failed and
logged ``email.send_failed``), and once SMTP is back the relay's next attempt (30 s base backoff)
delivers it to Mailpit. Nightly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
import pytest
from _stack import Bff, Containers, Stack, log_events, wait_until

SEND_WAIT_S = 120.0  # first retry after 30 s ± 10%, then the 2 s poll


def _outbox(stack: Stack, email: str) -> tuple[str, int] | None:
    rows = stack.psql(
        "SELECT e.status, e.attempts FROM identity.email_outbox e "
        "JOIN identity.invites i ON i.id = e.invite_id "
        f"JOIN identity.users u ON u.id = i.user_id WHERE u.email = '{email}'"
    )
    return (rows[0][0], int(rows[0][1])) if rows else None


@pytest.mark.timeout(SEND_WAIT_S + 120)
def test_smtp_down_queues_the_invite_and_sends_after_recovery(
    stack: Stack, containers: Containers, bff: Bff
) -> None:
    """PAY-110, HLD §4 "SMTP down": invite 201 while SMTP is down; the email is queued, not
    lost, and Mailpit receives it after recovery."""
    t0 = datetime.now(UTC)
    email = f"smtp-down-{uuid.uuid4().hex[:12]}@payrun.example.com"
    cookie = bff.login()
    containers.stop("mailpit")

    r = bff.call(
        "POST",
        "/api/users/invite",
        cookie=cookie,
        json={"email": email, "name": "SMTP Down", "roles": ["approver"]},
    )
    assert r.status_code == 201, r.status_code

    def failed_once() -> bool:
        row = _outbox(stack, email)
        return row is not None and row[0] == "pending" and row[1] >= 1

    wait_until("the first send attempt failed", failed_once, 30)
    assert log_events(stack.logs_since("identity", t0), "email.send_failed") >= 1

    containers.start("mailpit")
    mailpit = f"{stack.url('MAILPIT_PORT')}/api/v1/search?query={quote(f'to:"{email}"')}"

    def delivered() -> bool:
        return bool(httpx.get(mailpit, timeout=5).json().get("messages"))

    wait_until("the invite email in Mailpit", delivered, SEND_WAIT_S, interval=2)
    row = wait_until("the outbox row marked sent", lambda: _outbox(stack, email), 10)
    assert row[0] == "sent"
    assert row[1] >= 2  # the failed attempt, then the one that delivered

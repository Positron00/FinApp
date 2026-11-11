"""The redaction test (identity LLD §7.2 test_telemetry_redaction; PAY-NFR-07, PAY-NFR-08)."""

from __future__ import annotations

from typing import Any

from pycommon.testing import JsonLogCapture, OtelMemory

from tests.helpers import AUDIT_KEY
from tests.integration.conftest import CLIENT_IP, FakeSmtp, HarnessFactory, token_in, wait_until

ADMIN_PW = "admin passphrase 0001"
NEW_PW = "invitee passphrase 0002"
WRONG_PW = "not the passphrase 0003"
INVITEE = "secret.invitee@example.com"


def _span_text(otel: OtelMemory) -> str:
    parts: list[str] = []
    for span in otel.spans():
        parts.append(span.name)
        parts.append(repr(dict(span.attributes or {})))
        for event in span.events:
            parts.append(event.name)
            parts.append(repr(dict(event.attributes or {})))
    return "\n".join(parts)


def _otel_log_text(otel: OtelMemory) -> str:
    out = []
    for rec in otel.log_records():
        lr = getattr(rec, "log_record", rec)
        out.append(repr(getattr(lr, "body", "")))
        out.append(repr(dict(getattr(lr, "attributes", None) or {})))
    return "\n".join(out)


async def test_no_secret_reaches_logs_or_spans(
    harness: HarnessFactory,
    smtp_server: FakeSmtp,
    caplog_json: JsonLogCapture,
    otel_memory: OtelMemory,
    admin_db: Any,
) -> None:
    """PAY-NFR-07, PAY-NFR-08: invite → email → accept → failed and successful login, fully
    instrumented; no password, raw token, JWT, client IP, email, AUDIT_IP_HASH_KEY, DB or Redis
    password appears in any log line or span attribute."""
    smtp_server.messages.clear()
    jwts: list[str] = []
    async with harness(
        sender="smtp", run_relay=True, instrument=True, identity_email_poll_interval_s=0.05
    ) as h:
        admin = await h.create_user("admin.person@example.com", password=ADMIN_PW)
        headers = h.user_headers(admin, ("admin",))
        jwts.append(headers["Authorization"].removeprefix("Bearer "))
        r = await h.client.post(
            "/v1/users/invite",
            json={"email": INVITEE, "name": "Secret Invitee", "roles": ["approver"]},
            headers=headers,
        )
        assert r.status_code == 201
        await wait_until(lambda: len(smtp_server.messages) == 1)
        token = token_in(smtp_server.messages[0])
        web = h.web_headers()
        jwts.append(web["Authorization"].removeprefix("Bearer "))
        r = await h.client.post(
            "/v1/invites/accept", json={"token": token, "password": NEW_PW}, headers=web
        )
        assert r.status_code == 200
        assert (await h.login(INVITEE, WRONG_PW)).status_code == 401
        assert (await h.login(INVITEE, NEW_PW)).status_code == 200
        assert (await h.login("admin.person@example.com", ADMIN_PW)).status_code == 200
        s = h.rt.settings
        secrets = {
            "invite token": token,
            "admin password": ADMIN_PW,
            "new password": NEW_PW,
            "wrong password": WRONG_PW,
            "audit key": AUDIT_KEY,
            "db password": s.db_password.get_secret_value(),
            "redis password": s.redis_password.get_secret_value() if s.redis_password else "",
            "client ip": CLIENT_IP,
            "invitee email": INVITEE,
            "admin email": "admin.person@example.com",
            "user name": "Secret Invitee",
            **{f"jwt {i}": j for i, j in enumerate(jwts)},
        }

    logs = caplog_json.text
    spans = _span_text(otel_memory)
    otel_logs = _otel_log_text(otel_memory)
    assert "auth.signin.failed" in logs
    assert "identity.email.send" in spans
    assert "identity.login" in spans
    for label, value in secrets.items():
        assert value
        assert value not in logs, f"{label} in logs"
        assert value not in spans, f"{label} in spans"
        assert value not in otel_logs, f"{label} in OTel logs"
    # The audit trail holds hashes, never raw IPs or emails (PAY-NFR-08).
    dump = " ".join(str(dict(r)) for r in await admin_db.fetch("SELECT * FROM identity.audit_log"))
    assert CLIENT_IP not in dump
    assert INVITEE not in dump

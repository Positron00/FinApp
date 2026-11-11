"""The redaction test (identity LLD §7.2 test_telemetry_redaction; PAY-NFR-07, PAY-NFR-08)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pycommon.testing import JsonLogCapture, OtelMemory

from tests.helpers import AUDIT_KEY, passphrase
from tests.integration.conftest import (
    CLIENT_IP,
    FakeSmtp,
    Harness,
    HarnessFactory,
    token_in,
    wait_until,
)

ADMIN_PW = "admin passphrase 0001"
NEW_PW = "invitee passphrase 0002"
WRONG_PW = "not the passphrase 0003"
RESET_PW = passphrase()
INVITEE = "secret.invitee@example.com"

BASE_FIELDS = frozenset(
    {"ts", "level", "service", "env", "version", "logger", "event", "msg", "trace_id"}
    | {"span_id", "request_id"}
)
# identity LLD §5.1: the only fields each identity log event may carry.
ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "identity.bootstrap.completed": frozenset({"user_id", "company_id", "mode"}),
    "identity.bootstrap.refused": frozenset({"company_id"}),
    "identity.bootstrap.skipped": frozenset({"company_id"}),
    "identity.bootstrap.failed": frozenset({"reason", "detail", "error_type", "exc"}),
    "user.invited": frozenset({"user_id", "invite_id", "actor_user_id", "roles"}),
    "user.invite_resent": frozenset({"user_id", "invite_id", "revoked_invite_id", "actor_user_id"}),
    "user.invite_accepted": frozenset({"user_id", "invite_id"}),
    "invite.invalid": frozenset({"reason"}),
    "auth.signin.succeeded": frozenset({"user_id", "ip_hash", "rehashed"}),
    "auth.signin.failed": frozenset({"user_id", "reason", "ip_hash"}),
    "auth.signin.rate_limited": frozenset({"scope", "ip_hash", "retry_after_s"}),
    "auth.lockout": frozenset({"user_id", "lock_level", "lock_seconds", "ip_hash"}),
    "auth.forbidden": frozenset({"route", "reason"}),
    "auth.password_reset.requested": frozenset({"matched", "user_id", "reset_id", "ip_hash"}),
    "auth.password_reset.completed": frozenset({"user_id", "reset_id", "roles_version"}),
    "auth.password_reset.invalid": frozenset({"reason"}),
    "user.roles_changed": frozenset(
        {"user_id", "actor_user_id", "old_roles", "new_roles", "roles_version"}
    ),
    "user.deactivated": frozenset(
        {"user_id", "actor_user_id", "roles_version", "revoked_invite_id"}
    ),
    "user.update_refused": frozenset({"user_id", "actor_user_id", "code"}),
    "email.queued": frozenset({"email_id", "template", "user_id"}),
    "email.sent": frozenset({"email_id", "template", "attempts", "duration_ms"}),
    "email.send_failed": frozenset(
        {"email_id", "template", "attempts", "error", "next_attempt_in_s"}
    ),
    "email.failed": frozenset({"email_id", "template", "attempts", "error"}),
    "email.cancelled": frozenset({"email_id", "template", "reason"}),
    "email.relay.error": frozenset({"error"}),
    "ratelimit.backend_error": frozenset({"component", "fail_mode", "error_type"}),
}


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


async def _reset_and_admin_flow(
    h: Harness, smtp_server: FakeSmtp, admin: UUID, user_id: str, headers: dict[str, str]
) -> str:
    """The F1 part of the scenario: reset (bad, then good confirm), role change, self-PATCH
    refusal and deactivation. Returns the reset token."""
    assert (await h.reset_request(INVITEE)).status_code == 202
    await wait_until(lambda: len(smtp_server.messages) == 2)
    reset_token = token_in(smtp_server.messages[1])
    assert (await h.reset_confirm(reset_token[:-1], RESET_PW)).status_code == 400
    assert (await h.reset_confirm(reset_token, RESET_PW)).status_code == 200
    r = await h.client.patch(
        f"/v1/users/{user_id}", json={"roles": ["payroll_admin"]}, headers=headers
    )
    assert r.status_code == 200
    r = await h.client.patch(f"/v1/users/{admin}", json={"roles": ["admin"]}, headers=headers)
    assert r.status_code == 409
    r = await h.client.patch(f"/v1/users/{user_id}", json={"status": "inactive"}, headers=headers)
    assert r.status_code == 200
    return reset_token


async def test_no_secret_reaches_logs_or_spans(
    harness: HarnessFactory,
    smtp_server: FakeSmtp,
    caplog_json: JsonLogCapture,
    otel_memory: OtelMemory,
    admin_db: Any,
) -> None:
    """PAY-NFR-07, PAY-NFR-08: invite → resend → email → accept → failed and successful login →
    password reset (an invalid confirm, then a good one) → role change, self-PATCH and
    deactivation, fully instrumented; no password, raw token, JWT, client IP, email,
    AUDIT_IP_HASH_KEY, DB or Redis password appears in any log line or span attribute, and every
    identity log event carries only its §5.1 fields."""
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
        user_id = r.json()["id"]
        r = await h.client.post(
            f"/v1/invites/{r.json()['pending_invite_id']}/resend", headers=headers
        )
        assert r.status_code == 200
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
        reset_token = await _reset_and_admin_flow(h, smtp_server, admin, user_id, headers)
        s = h.rt.settings
        secrets = {
            "invite token": token,
            "reset token": reset_token,
            "reset password": RESET_PW,
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
    # Every identity log line uses a §5.1 event with only its allow-listed fields.
    own = [r for r in caplog_json.records() if str(r["logger"]).startswith("identity.")]
    assert own
    for rec in own:
        allowed = ALLOWED_FIELDS.get(rec["event"])
        assert allowed is not None, f"unlisted identity event {rec['event']}"
        extra = set(rec) - BASE_FIELDS - allowed
        assert not extra, f"{rec['event']} has unlisted fields {sorted(extra)}"
    spans = _span_text(otel_memory)
    otel_logs = _otel_log_text(otel_memory)
    assert "auth.signin.failed" in logs
    for event in (
        "user.invite_resent",
        "auth.password_reset.requested",
        "auth.password_reset.invalid",
        "auth.password_reset.completed",
        "user.roles_changed",
        "user.update_refused",
        "user.deactivated",
    ):
        assert caplog_json.events(event), event
    assert "identity.reset.confirm" in _span_text(otel_memory)
    assert "identity.user.update" in _span_text(otel_memory)
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

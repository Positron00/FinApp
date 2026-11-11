"""Email rendering (identity LLD §3.9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from identity.email.templates import build_message, link_for, render
from identity.settings import IdentitySettings

from tests.helpers import make_settings

TOKEN = "A" * 43
EXPIRES = datetime(2026, 11, 13, 9, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("template", "path"), [("invite", "/accept#token="), ("password_reset", "/reset#token=")]
)
def test_links_carry_the_token_in_the_fragment(template: str, path: str) -> None:
    """PAY-103, PAY-105, PAY-NFR-07: {base}/accept#token=… and {base}/reset#token=…, no '?'."""
    link = link_for(template, TOKEN, "http://127.0.0.1:4100/")  # type: ignore[arg-type]
    assert link == f"http://127.0.0.1:4100{path}{TOKEN}"
    assert "?" not in link


def test_settings_strip_the_base_url_trailing_slash() -> None:
    """PAY-103: IDENTITY_PUBLIC_BASE_URL has no trailing slash (validator strips it)."""
    s: IdentitySettings = make_settings(identity_public_base_url=" https://pay.example/ ")
    assert s.identity_public_base_url == "https://pay.example"


def test_invite_render_text_and_html() -> None:
    """PAY-101, PAY-103: text and HTML carry the link and expiry; HTML escapes the name."""
    link = link_for("invite", TOKEN, "http://127.0.0.1:4100")
    r = render("invite", name="<b>Ann</b> & co", link=link, expires_at=EXPIRES)
    assert r.subject == "You're invited to PayRun"
    assert TOKEN not in r.subject
    assert link in r.text
    assert "2026-11-13 09:05 UTC" in r.text
    assert "<b>Ann</b> & co" in r.text
    assert "&lt;b&gt;Ann&lt;/b&gt; &amp; co" in r.html
    assert f'<a href="{link}">' in r.html
    assert TOKEN not in repr(r)


def test_reset_render_converts_expiry_to_utc() -> None:
    """PAY-105: expires_at is shown in UTC."""
    local = EXPIRES.astimezone(timezone(timedelta(hours=-5)))
    link = link_for("password_reset", TOKEN, "http://h")
    r = render("password_reset", name="Ann", link=link, expires_at=local)
    assert r.subject == "Reset your PayRun password"
    assert "2026-11-13 09:05 UTC" in r.text


def test_build_message_headers_and_parts() -> None:
    """PAY-110: multipart/alternative with From, To, Subject, Auto-Submitted and Message-ID."""
    r = render("invite", name="Ann", link=link_for("invite", TOKEN, "http://h"), expires_at=EXPIRES)
    msg = build_message(
        r, sender="PayRun <no-reply@payrun.example.com>", recipient="ann@example.com"
    )
    assert msg["From"] == "PayRun <no-reply@payrun.example.com>"
    assert msg["To"] == "ann@example.com"
    assert msg["Subject"] == "You're invited to PayRun"
    assert msg["Auto-Submitted"] == "auto-generated"
    assert msg["Message-ID"].endswith("@payrun.example.com>")
    assert msg.get_content_type() == "multipart/alternative"
    kinds = [p.get_content_type() for p in msg.iter_parts()]
    assert kinds == ["text/plain", "text/html"]


def test_build_message_without_domain_in_sender() -> None:
    """PAY-110: a sender without a domain still gets a Message-ID."""
    r = render("invite", name="Ann", link="http://h/accept#token=x", expires_at=EXPIRES)
    msg = build_message(r, sender="payrun", recipient="ann@example.com")
    assert msg["Message-ID"].endswith("@payrun.example.com>")

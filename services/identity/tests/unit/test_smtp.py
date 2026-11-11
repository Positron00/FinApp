"""SMTP sender behind pycommon Guard (identity LLD §1, §6.9)."""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any

import aiosmtplib
import pytest
from identity.email.smtp import SmtpSender, error_label, is_transient
from pycommon.http import ProviderTimeout, ProviderUnavailable

from tests.helpers import make_settings


def test_transient_classification() -> None:
    """PAY-110: 4xx replies and SMTP protocol errors are transient; 5xx replies are not."""
    assert is_transient(aiosmtplib.SMTPResponseException(451, "later"))
    assert not is_transient(aiosmtplib.SMTPResponseException(550, "no"))
    assert is_transient(aiosmtplib.SMTPServerDisconnected("gone"))
    assert not is_transient(ValueError("x"))


def test_error_label_never_has_message_text() -> None:
    """PAY-110, PAY-NFR-07: last_error is the reply code or the class name only."""
    assert error_label(aiosmtplib.SMTPResponseException(451, "secret text")) == "smtp_451"
    wrapped = ProviderUnavailable("smtp", "send", "connect_error")
    wrapped.__cause__ = aiosmtplib.SMTPConnectError("secret")
    assert error_label(wrapped) == "SMTPConnectError"
    plain = ProviderUnavailable("smtp", "send", "connect_error")
    plain.__cause__ = ConnectionRefusedError("x")
    assert error_label(plain) == "ConnectionRefusedError"
    assert error_label(ValueError("x")) == "ValueError"


async def test_send_passes_settings_to_aiosmtplib(monkeypatch: pytest.MonkeyPatch) -> None:
    """PAY-110: host, port, STARTTLS, credentials and timeout come from settings."""
    seen: dict[str, Any] = {}

    async def fake_send(msg: EmailMessage, **kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    from pydantic import SecretStr  # noqa: PLC0415

    s = make_settings(
        smtp_host="mx",
        smtp_port=587,
        smtp_starttls=True,
        smtp_username="u",
        smtp_password=SecretStr("p" * 20),
    )
    await SmtpSender(s).send(EmailMessage())
    assert seen["hostname"] == "mx"
    assert seen["port"] == 587
    assert seen["start_tls"] is True
    assert seen["username"] == "u"
    assert seen["password"] == "p" * 20
    assert seen["timeout"] == s.identity_smtp_timeout_s


async def test_connect_errors_open_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """PAY-110: no inner retries; after BREAKER_FAILURES connect errors the breaker opens."""
    calls = 0

    async def failing(msg: EmailMessage, **kw: Any) -> None:
        nonlocal calls
        calls += 1
        raise aiosmtplib.SMTPConnectError("down")

    monkeypatch.setattr(aiosmtplib, "send", failing)
    sender = SmtpSender(make_settings(breaker_failures=2))
    for _ in range(2):
        with pytest.raises(ProviderUnavailable) as exc:
            await sender.send(EmailMessage())
        assert exc.value.reason == "connect_error"
    with pytest.raises(ProviderUnavailable) as exc:
        await sender.send(EmailMessage())
    assert exc.value.reason == "breaker_open"
    assert calls == 2


async def test_timeouts_and_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    """PAY-110: a timeout is ProviderTimeout; an SMTP reply error is re-raised as itself."""

    async def timing_out(msg: EmailMessage, **kw: Any) -> None:
        raise aiosmtplib.SMTPTimeoutError("slow")

    monkeypatch.setattr(aiosmtplib, "send", timing_out)
    with pytest.raises(ProviderTimeout):
        await SmtpSender(make_settings()).send(EmailMessage())

    async def refused(msg: EmailMessage, **kw: Any) -> None:
        raise aiosmtplib.SMTPResponseException(550, "no such user")

    monkeypatch.setattr(aiosmtplib, "send", refused)
    with pytest.raises(aiosmtplib.SMTPResponseException):
        await SmtpSender(make_settings()).send(EmailMessage())

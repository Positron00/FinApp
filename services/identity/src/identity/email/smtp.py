"""SMTP sender behind pycommon ``Guard`` (identity LLD §1, §6.9; PAY-110).

One attempt per call (``max_retries=0``): the relay owns the backoff. The Guard gives the breaker
(``circuit_breaker_state{target="smtp"}``) and ``external_requests_total`` /
``external_request_duration_seconds`` with ``provider="smtp"``, ``operation="send"``.
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import Protocol

import aiosmtplib
from pycommon import Clock
from pycommon.http import BreakerState, Guard, ProviderPolicy

from identity.settings import IdentitySettings

__all__ = ["EmailSender", "SmtpSender", "error_label", "is_transient"]


class EmailSender(Protocol):
    def available(self) -> bool:
        """False while the breaker is open, so the relay doesn't issue a token it can't send."""
        ...

    async def send(self, msg: EmailMessage) -> None: ...


def is_transient(exc: BaseException) -> bool:
    """4xx SMTP replies and dropped connections are transient; 5xx replies are permanent."""
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        return 400 <= exc.code < 500
    return isinstance(exc, aiosmtplib.SMTPException)


def error_label(exc: BaseException) -> str:
    """``smtp_<code>`` for an SMTP reply, else the error class (never message text)."""
    cause: BaseException = exc
    while cause.__cause__ is not None and not isinstance(cause, aiosmtplib.SMTPException):
        cause = cause.__cause__
    if isinstance(cause, aiosmtplib.SMTPResponseException) and cause.code > 0:
        return f"smtp_{cause.code}"
    return type(cause).__name__[:64]


class SmtpSender:
    def __init__(self, settings: IdentitySettings, *, clock: Clock | None = None) -> None:
        s = settings
        self._s = s
        self.guard = Guard(
            ProviderPolicy(
                name="smtp",
                max_retries=0,
                total_deadline=s.identity_smtp_timeout_s,
                breaker_failures=s.breaker_failures,
                breaker_reset_s=s.breaker_reset_s,
            ),
            clock=clock,
        )

    async def _send_once(self, msg: EmailMessage) -> None:
        s = self._s
        await aiosmtplib.send(
            msg,
            hostname=s.smtp_host,
            port=s.smtp_port,
            username=s.smtp_username,
            password=s.smtp_password.get_secret_value() if s.smtp_password else None,
            start_tls=s.smtp_starttls,
            use_tls=False,
            timeout=s.identity_smtp_timeout_s,
        )

    def available(self) -> bool:
        # peek_state doesn't consume the half-open trial: the send itself takes it.
        return self.guard.breaker.peek_state() is not BreakerState.open

    async def send(self, msg: EmailMessage) -> None:
        await self.guard.call("send", lambda: self._send_once(msg), retryable=is_transient)

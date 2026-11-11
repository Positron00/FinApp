"""The email relay (identity LLD §6.9, §6.7; PAY-110, R17, C31).

Claims due outbox rows with ``SKIP LOCKED``, issues a fresh token per send (only its hash is
stored), renders, sends through ``SmtpSender`` and records the outcome. The raw token, the link
and the rendered body are local variables only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Final, cast

from pycommon.db import transaction
from pycommon.http import ProviderUnavailable
from pycommon.logging import get_logger, log_event
from pycommon.otel import context_from_traceparent, tracer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity.db.repositories import ClaimedEmail, InviteRepo, IssuedFor, OutboxRepo, ResetTokenRepo
from identity.domain.email_schedule import is_exhausted, retry_delay_s
from identity.domain.tokens import new_token
from identity.email.smtp import EmailSender, error_label
from identity.email.templates import Template, build_message, link_for, render
from identity.metrics import IdentityMetrics
from identity.settings import IdentitySettings

__all__ = ["ERROR_PAUSE_S", "EmailRelay"]

ERROR_PAUSE_S: Final = 5.0

_log = get_logger("identity.email.relay")


async def _sleep_or_stop(seconds: float, stop: asyncio.Event) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


class EmailRelay:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: IdentitySettings,
        sender: EmailSender,
        metrics: IdentityMetrics,
        *,
        delay_fn: Callable[[int], int] | None = None,
    ) -> None:
        s = settings
        self._sf = sessions
        self._s = s
        self._sender = sender
        self._metrics = metrics
        self._delay = delay_fn or (
            lambda n: retry_delay_s(n, s.identity_email_retry_base_s, s.identity_email_retry_max_s)
        )

    async def run(self, stop: asyncio.Event) -> None:
        """Until ``stop``: claim and send; idle ``IDENTITY_EMAIL_POLL_INTERVAL_S`` when drained."""
        while not stop.is_set():
            try:
                handled = await self.run_once(stop)
            except Exception as exc:
                log_event(_log, logging.ERROR, "email.relay.error", error=type(exc).__name__)
                await _sleep_or_stop(ERROR_PAUSE_S, stop)
                continue
            if handled < self._s.identity_email_batch:
                await _sleep_or_stop(self._s.identity_email_poll_interval_s, stop)

    async def run_once(self, stop: asyncio.Event | None = None) -> int:
        """Claim one batch and process it; returns the number of rows claimed."""
        with tracer("identity").start_as_current_span("identity.email.claim") as span:
            async with transaction(self._sf) as session:
                rows = await OutboxRepo(session).claim(
                    batch=self._s.identity_email_batch, lease_s=self._s.identity_email_lease_s
                )
            span.set_attribute("identity.batch_size", len(rows))
        for row in rows:
            if stop is not None and stop.is_set():
                break  # the rest are retried after their lease
            await self._process(row)
        return len(rows)

    async def _issue(self, row: ClaimedEmail, digest: bytes) -> IssuedFor | None:
        async with transaction(self._sf) as session:
            if row.template == "invite" and row.invite_id is not None:
                return await InviteRepo(session).issue_token(
                    row.invite_id, digest, self._s.invite_ttl_h
                )
            if row.template == "password_reset" and row.password_reset_id is not None:
                return await ResetTokenRepo(session).issue_token(
                    row.password_reset_id, digest, self._s.reset_ttl_h
                )
        return None  # pragma: no cover  (ck_email_outbox_ref makes this unreachable)

    async def _finish(self, row: ClaimedEmail, status: str, last_error: str | None = None) -> None:
        async with transaction(self._sf) as session:
            await OutboxRepo(session).finish(row.id, status, last_error=last_error)

    async def _process(self, row: ClaimedEmail) -> None:
        template = cast(Template, row.template)
        token = new_token()
        issued = await self._issue(row, token.digest)
        if issued is None:
            await self._finish(row, "cancelled")
            reason = "invite_not_pending" if template == "invite" else "reset_not_pending"
            log_event(
                _log,
                logging.INFO,
                "email.cancelled",
                email_id=str(row.id),
                template=template,
                reason=reason,
            )
            return
        rendered = render(
            template,
            name=issued.name,
            link=link_for(template, token.raw, self._s.identity_public_base_url),
            expires_at=issued.expires_at,
        )
        msg = build_message(rendered, sender=self._s.identity_email_from, recipient=row.recipient)
        del token, rendered
        started = time.monotonic()
        with tracer("identity").start_as_current_span(
            "identity.email.send",
            context=context_from_traceparent(row.traceparent),
            attributes={
                "identity.email_id": str(row.id),
                "identity.template": template,
                "identity.attempt": row.attempts,
            },
        ):
            try:
                await self._sender.send(msg)
            except ProviderUnavailable as exc:
                if exc.reason != "breaker_open":
                    await self._failed_attempt(row, exc)
                    return
                async with transaction(self._sf) as session:
                    await OutboxRepo(session).release_uncounted(row.id, self._s.breaker_reset_s)
                return
            except Exception as exc:
                await self._failed_attempt(row, exc)
                return
        await self._finish(row, "sent")
        log_event(
            _log,
            logging.INFO,
            "email.sent",
            email_id=str(row.id),
            template=template,
            attempts=row.attempts,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
        )

    async def _failed_attempt(self, row: ClaimedEmail, exc: BaseException) -> None:
        label = error_label(exc)
        if is_exhausted(row.attempts, self._s.email_max_attempts):
            await self._finish(row, "failed", last_error=label)
            self._metrics.emails_failed.add(1)
            log_event(
                _log,
                logging.ERROR,
                "email.failed",
                email_id=str(row.id),
                template=row.template,
                attempts=row.attempts,
                error=label,
            )
            return
        delay = self._delay(row.attempts)
        async with transaction(self._sf) as session:
            await OutboxRepo(session).retry_in(row.id, delay, last_error=label)
        log_event(
            _log,
            logging.WARNING,
            "email.send_failed",
            email_id=str(row.id),
            template=row.template,
            attempts=row.attempts,
            error=label,
            next_attempt_in_s=delay,
        )

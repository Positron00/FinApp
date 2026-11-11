"""The relay loop's control flow (identity LLD §6.9; PAY-110)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from identity.email import relay as relay_mod
from identity.email.relay import EmailRelay
from identity.metrics import create_metrics
from pycommon.testing import JsonLogCapture

from tests.helpers import make_settings


class ScriptedRelay(EmailRelay):
    """run_once returns or raises from a script; stops the loop when the script ends."""

    def __init__(self, script: list[Any], stop: asyncio.Event, **settings: Any) -> None:
        super().__init__(None, make_settings(**settings), None, create_metrics())  # type: ignore[arg-type]
        self.script = script
        self.stop = stop
        self.calls = 0

    async def run_once(self, stop: asyncio.Event | None = None) -> int:
        self.calls += 1
        step = self.script.pop(0)
        if not self.script:
            self.stop.set()
        if isinstance(step, Exception):
            raise step
        return int(step)


async def test_errors_are_logged_and_the_loop_continues(
    monkeypatch: pytest.MonkeyPatch, caplog_json: JsonLogCapture
) -> None:
    """PAY-110: an unexpected error logs email.relay.error (class only) and the loop goes on;
    a full batch is followed at once by another claim."""
    monkeypatch.setattr(relay_mod, "ERROR_PAUSE_S", 0.0)
    stop = asyncio.Event()
    relay = ScriptedRelay(
        [RuntimeError("secret detail"), 10, 0],
        stop,
        identity_email_batch=10,
        identity_email_poll_interval_s=0.0,
    )
    await asyncio.wait_for(relay.run(stop), timeout=5)
    assert relay.calls == 3
    [err] = caplog_json.events("email.relay.error")
    assert err["error"] == "RuntimeError"
    assert "secret detail" not in caplog_json.text


async def test_stop_before_start_returns_at_once() -> None:
    """PAY-110: with stop already set the relay claims nothing."""
    stop = asyncio.Event()
    stop.set()
    relay = ScriptedRelay([0], stop)
    await relay.run(stop)
    assert relay.calls == 0

"""Resilient outbound calls: deadline, retries, circuit breaker, metrics (platform LLD §3.10).

Bodies, headers and query strings are never logged or put on spans.
"""

# ruff: noqa: N818  (exception names are the platform LLD contract)

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import threading
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any, Final, Literal

import httpx
from opentelemetry import trace

from pycommon import Clock, SystemClock
from pycommon.logging import get_logger, log_event
from pycommon.otel import SECONDS_BUCKETS, Observation, histogram, meter, observable_gauge, tracer

__all__ = [
    "BreakerState",
    "CircuitBreaker",
    "Guard",
    "ProviderError",
    "ProviderPolicy",
    "ProviderRateLimited",
    "ProviderRejected",
    "ProviderTimeout",
    "ProviderUnavailable",
    "ResilientClient",
]

_log = get_logger("pycommon.http")
_rng = random.SystemRandom()

IDEMPOTENT_METHODS: Final = frozenset({"GET", "HEAD", "PUT", "DELETE"})

Outcome = Literal[
    "ok", "client_error", "rate_limited", "server_error", "timeout", "connect_error", "breaker_open"
]


@dataclass(frozen=True)
class ProviderPolicy:
    name: str
    base_url: str = ""
    connect_timeout: float = 1.0
    read_timeout: float = 5.0
    total_deadline: float = 15.0
    max_retries: int = 3
    retry_on: frozenset[int] = frozenset({429, 502, 503, 504})
    retry_on_timeout: bool = True
    backoff_base_s: float = 0.2
    backoff_cap_s: float = 2.0
    breaker_failures: int = 5
    breaker_reset_s: float = 30.0


class BreakerState(IntEnum):
    closed = 0
    half_open = 1
    open = 2


# --- circuit_breaker_state gauge: one instrument per meter provider, fed by every breaker -------

_BREAKERS: weakref.WeakSet[CircuitBreaker] = weakref.WeakSet()
_BREAKERS_LOCK = threading.Lock()


def _breaker_observations() -> Observation:
    with _BREAKERS_LOCK:
        breakers = list(_BREAKERS)
    return {frozenset({("target", b.target)}): float(b.peek_state()) for b in breakers}


def _ensure_breaker_gauge() -> None:
    # Idempotent: observable_gauge keeps one instrument per meter provider and name.
    observable_gauge(
        "circuit_breaker_state",
        unit="{state}",
        description="Circuit breaker state per target: 0 closed, 1 half-open, 2 open",
        callback=_breaker_observations,
    )


class CircuitBreaker:
    """Consecutive-failure breaker: closed → open → half-open (one trial) → closed or open."""

    def __init__(
        self, target: str, *, failures: int, reset_s: float, clock: Clock | None = None
    ) -> None:
        self.target = target
        self._threshold = failures
        self._reset_s = reset_s
        self._clock = clock or SystemClock()
        self._state = BreakerState.closed
        self._failures = 0
        self._opened_at = 0.0
        self._trial_in_flight = False
        with _BREAKERS_LOCK:
            _BREAKERS.add(self)
        _ensure_breaker_gauge()

    def peek_state(self) -> BreakerState:
        """The current state without performing (or logging) a transition."""
        if (
            self._state is BreakerState.open
            and self._clock.monotonic() - self._opened_at >= self._reset_s
        ):
            return BreakerState.half_open
        return self._state

    @property
    def state(self) -> BreakerState:
        if self.peek_state() is not self._state:
            self._transition(BreakerState.half_open)
        return self._state

    def _transition(self, new: BreakerState) -> None:
        if new is self._state:
            return
        self._state = new
        if new is BreakerState.open:
            self._opened_at = self._clock.monotonic()
            self._trial_in_flight = False
            log_event(_log, logging.WARNING, "external.breaker.opened", target=self.target)
        elif new is BreakerState.half_open:
            self._trial_in_flight = False
            log_event(_log, logging.INFO, "external.breaker.half_open", target=self.target)
        else:
            self._failures = 0
            self._trial_in_flight = False
            log_event(_log, logging.INFO, "external.breaker.closed", target=self.target)

    def allow(self) -> bool:
        state = self.state
        if state is BreakerState.closed:
            return True
        if state is BreakerState.open:
            return False
        if self._trial_in_flight:
            return False
        self._trial_in_flight = True
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._transition(BreakerState.closed)

    def record_failure(self) -> None:
        state = self.state
        if state is BreakerState.half_open:
            self._transition(BreakerState.open)
            return
        if state is BreakerState.open:
            return
        self._failures += 1
        if self._failures >= self._threshold:
            self._transition(BreakerState.open)


class ProviderError(Exception):
    def __init__(self, provider: str, operation: str, message: str = "") -> None:
        self.provider = provider
        self.operation = operation
        super().__init__(message or f"{provider} {operation} failed")


class ProviderTimeout(ProviderError):
    """A connect or read timeout, or the total deadline."""


class ProviderRateLimited(ProviderError):
    def __init__(self, provider: str, operation: str, retry_after_s: float | None = None) -> None:
        self.retry_after_s = retry_after_s
        super().__init__(provider, operation, f"{provider} {operation} rate limited")


class ProviderUnavailable(ProviderError):
    def __init__(
        self,
        provider: str,
        operation: str,
        reason: Literal["breaker_open", "connect_error", "server_error"],
    ) -> None:
        self.reason: Literal["breaker_open", "connect_error", "server_error"] = reason
        super().__init__(provider, operation, f"{provider} {operation} unavailable: {reason}")


class ProviderRejected(ProviderError):
    def __init__(self, provider: str, operation: str, status: int) -> None:
        self.status = status
        super().__init__(provider, operation, f"{provider} {operation} rejected: {status}")


@dataclass
class _Attempt[T]:
    outcome: Outcome
    value: T | None = None
    error: BaseException | None = None
    status: int | None = None
    retry_after_s: float | None = None
    retryable: bool = False
    breaker_failure: bool = False


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """``Retry-After`` in seconds (delta-seconds or an HTTP date), or ``None``."""
    if not value:
        return None
    v = value.strip()
    if v.isdigit():
        return float(v)
    try:
        when = email.utils.parsedate_to_datetime(v)
    except TypeError, ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


class _Engine:
    """Deadline, retries, breaker, per-attempt span and metrics; shared by Guard and the client."""

    def __init__(self, policy: ProviderPolicy, clock: Clock) -> None:
        self.policy = policy
        self.clock = clock
        self.breaker = CircuitBreaker(
            policy.name,
            failures=policy.breaker_failures,
            reset_s=policy.breaker_reset_s,
            clock=clock,
        )
        self._requests = meter().create_counter(
            "external_requests", unit="1", description="Outbound calls per attempt and outcome"
        )
        self._duration = histogram(
            "external_request_duration_seconds",
            unit="s",
            description="Outbound call duration per attempt",
            buckets=SECONDS_BUCKETS,
        )

    def backoff(self, retry_index: int) -> float:
        p = self.policy
        return _rng.uniform(0, min(p.backoff_cap_s, p.backoff_base_s * 2**retry_index))

    def _record(self, operation: str, outcome: Outcome, duration_s: float | None) -> None:
        attrs = {"provider": self.policy.name, "operation": operation}
        self._requests.add(1, {**attrs, "outcome": outcome})
        if duration_s is not None:
            self._duration.record(max(0.0, duration_s), attrs)

    def _failed(self, operation: str, attempt: int, reason: str) -> None:
        log_event(
            _log,
            logging.WARNING,
            "external.request.failed",
            provider=self.policy.name,
            operation=operation,
            attempt=attempt,
            reason=reason,
        )

    async def run[T](
        self,
        operation: str,
        attempt_fn: Callable[[int], Awaitable[_Attempt[T]]],
        deadline_s: float | None,
    ) -> _Attempt[T]:
        name = self.policy.name
        deadline = deadline_s or self.policy.total_deadline
        start = self.clock.monotonic()
        n = 0
        t0 = start
        in_flight = False
        cm = asyncio.timeout(deadline)
        try:
            async with cm:
                while True:
                    n += 1
                    if not self.breaker.allow():
                        self._record(operation, "breaker_open", None)
                        self._failed(operation, n, "breaker_open")
                        raise ProviderUnavailable(name, operation, "breaker_open")
                    t0 = self.clock.monotonic()
                    in_flight = True
                    with tracer().start_as_current_span(
                        f"{name} {operation}",
                        kind=trace.SpanKind.CLIENT,
                        attributes={"provider": name, "operation": operation, "attempt": n},
                    ) as span:
                        res = await attempt_fn(n)
                        if res.status is not None:
                            span.set_attribute("http.response.status_code", res.status)
                        if res.outcome in {"server_error", "timeout", "connect_error"}:
                            span.set_status(trace.Status(trace.StatusCode.ERROR, res.outcome))
                    in_flight = False
                    self._record(operation, res.outcome, self.clock.monotonic() - t0)
                    if res.breaker_failure:
                        self.breaker.record_failure()
                    else:
                        self.breaker.record_success()
                    if res.outcome == "ok":
                        return res
                    reason = str(res.status) if res.status is not None else res.outcome
                    if not res.retryable or n > self.policy.max_retries:
                        self._failed(operation, n, reason)
                        return res
                    delay = (
                        res.retry_after_s if res.retry_after_s is not None else self.backoff(n - 1)
                    )
                    remaining = deadline - (self.clock.monotonic() - start)
                    if delay >= remaining:
                        self._failed(operation, n, "deadline")
                        return res
                    log_event(
                        _log,
                        logging.WARNING,
                        "external.request.retry",
                        provider=name,
                        operation=operation,
                        attempt=n,
                        reason=reason,
                        delay_ms=int(delay * 1000),
                    )
                    await self.clock.sleep(delay)
        except TimeoutError:
            if not cm.expired():
                raise
            if in_flight:
                self._record(operation, "timeout", self.clock.monotonic() - t0)
                self.breaker.record_failure()
            self._failed(operation, n, "deadline")
            raise ProviderTimeout(name, operation) from None


class Guard:
    """Deadline + retries + breaker + metrics for any async call (used directly for SMTP)."""

    def __init__(self, policy: ProviderPolicy, *, clock: Clock | None = None) -> None:
        self.policy = policy
        self._engine = _Engine(policy, clock or SystemClock())

    @property
    def breaker(self) -> CircuitBreaker:
        return self._engine.breaker

    async def call[T](
        self,
        operation: str,
        fn: Callable[[], Awaitable[T]],
        *,
        retryable: Callable[[BaseException], bool],
        deadline_s: float | None = None,
    ) -> T:
        async def attempt(_n: int) -> _Attempt[T]:
            try:
                value = await fn()
            except TimeoutError as exc:
                return _Attempt(
                    "timeout",
                    error=exc,
                    retryable=self.policy.retry_on_timeout and retryable(exc),
                    breaker_failure=True,
                )
            except OSError as exc:
                return _Attempt(
                    "connect_error",
                    error=exc,
                    retryable=self.policy.retry_on_timeout and retryable(exc),
                    breaker_failure=True,
                )
            except Exception as exc:
                transient = retryable(exc)
                return _Attempt(
                    "server_error" if transient else "client_error",
                    error=exc,
                    retryable=transient,
                    breaker_failure=transient,
                )
            return _Attempt("ok", value=value)

        res = await self._engine.run(operation, attempt, deadline_s)
        if res.outcome == "ok":
            return res.value  # type: ignore[return-value]
        if res.outcome == "timeout":
            raise ProviderTimeout(self.policy.name, operation) from res.error
        if res.outcome == "connect_error":
            raise ProviderUnavailable(self.policy.name, operation, "connect_error") from res.error
        raise res.error or ProviderError(self.policy.name, operation)


class ResilientClient:
    """One instance per provider; see the behaviour list in platform LLD §3.10."""

    def __init__(
        self,
        policy: ProviderPolicy,
        *,
        auth_headers: Callable[[], Awaitable[Mapping[str, str]]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.policy = policy
        self._clock = clock or SystemClock()
        self._engine = _Engine(policy, self._clock)
        self._auth_headers = auth_headers
        timeout = httpx.Timeout(
            connect=policy.connect_timeout,
            read=policy.read_timeout,
            write=policy.read_timeout,
            pool=policy.connect_timeout,
        )
        self._client = httpx.AsyncClient(
            base_url=policy.base_url, timeout=timeout, transport=transport, follow_redirects=False
        )

    @property
    def breaker(self) -> CircuitBreaker:
        return self._engine.breaker

    async def _send(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json: Any,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        idempotency_key: str | None,
        deadline_s: float | None,
    ) -> _Attempt[httpx.Response]:
        m = method.upper()
        can_retry = m in IDEMPOTENT_METHODS or idempotency_key is not None
        p = self.policy

        async def attempt(_n: int) -> _Attempt[httpx.Response]:
            hdrs = dict(headers or {})
            if self._auth_headers is not None:
                hdrs.update(await self._auth_headers())
            if idempotency_key is not None:
                hdrs["Idempotency-Key"] = idempotency_key
            try:
                resp = await self._client.request(m, path, json=json, params=params, headers=hdrs)
            except httpx.TimeoutException as exc:
                return _Attempt(
                    "timeout",
                    error=exc,
                    retryable=can_retry and p.retry_on_timeout,
                    breaker_failure=True,
                )
            except httpx.TransportError as exc:
                return _Attempt(
                    "connect_error",
                    error=exc,
                    retryable=can_retry and p.retry_on_timeout,
                    breaker_failure=True,
                )
            status = resp.status_code
            outcome: Outcome
            if 200 <= status < 300:
                outcome = "ok"
            elif status == 429:
                outcome = "rate_limited"
            elif status >= 500:
                outcome = "server_error"
            else:
                outcome = "client_error"
            retry_after = parse_retry_after(resp.headers.get("retry-after"), self._clock.now())
            return _Attempt(
                outcome,
                value=resp,
                status=status,
                retry_after_s=retry_after if status in {429, 503} else None,
                retryable=can_retry and status in p.retry_on,
                breaker_failure=status >= 500,
            )

        return await self._engine.run(operation, attempt, deadline_s)

    def _response(self, res: _Attempt[httpx.Response], operation: str) -> httpx.Response:
        if res.outcome == "timeout":
            raise ProviderTimeout(self.policy.name, operation) from res.error
        if res.value is None:
            raise ProviderUnavailable(self.policy.name, operation, "connect_error") from res.error
        return res.value

    async def request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json: Any = None,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        deadline_s: float | None = None,
    ) -> httpx.Response:
        """Like ``request_raw``, then 2xx → response; 429, 5xx and other 4xx → typed errors."""
        res = await self._send(
            method,
            path,
            operation=operation,
            json=json,
            params=params,
            headers=headers,
            idempotency_key=idempotency_key,
            deadline_s=deadline_s,
        )
        resp = self._response(res, operation)
        name = self.policy.name
        if 200 <= resp.status_code < 300:
            return resp
        if resp.status_code == 429:
            retry_after = parse_retry_after(resp.headers.get("retry-after"), self._clock.now())
            raise ProviderRateLimited(name, operation, retry_after)
        if resp.status_code >= 500:
            raise ProviderUnavailable(name, operation, "server_error")
        raise ProviderRejected(name, operation, resp.status_code)

    async def request_raw(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json: Any = None,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        deadline_s: float | None = None,
    ) -> httpx.Response:
        """The final response, unmapped; raises only for timeouts, connect errors, open breaker."""
        res = await self._send(
            method,
            path,
            operation=operation,
            json=json,
            params=params,
            headers=headers,
            idempotency_key=idempotency_key,
            deadline_s=deadline_s,
        )
        return self._response(res, operation)

    async def aclose(self) -> None:
        await self._client.aclose()

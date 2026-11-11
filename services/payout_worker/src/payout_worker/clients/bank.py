"""``BankClient``: transfer create and lookup over ``ResilientClient.request_raw`` (design §3.5;
PAY-404, PAY-412, ADR 0003 a and e).

Never raises for an HTTP status and never maps a 4xx to ``ProviderRejected``: it returns the last
response or the transport error, and ``classify.py`` decides. The same ``Idempotency-Key`` is sent
on every retry. The API key and the body's bank details are never logged.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import quote

import httpx
from pycommon import Clock
from pycommon.http import ProviderError, ProviderPolicy, ResilientClient

from payout_worker.classify import BankCallResult
from payout_worker.models import TransferRequest
from payout_worker.settings import Settings

__all__ = ["BankClient", "bank_policy"]

PROVIDER: Final = "bank"


def bank_policy(s: Settings) -> ProviderPolicy:
    return ProviderPolicy(
        name=PROVIDER,
        base_url=s.bank_base_url,
        connect_timeout=s.bank_connect_timeout_s,
        read_timeout=s.bank_read_timeout_s,
        total_deadline=s.bank_total_deadline_s,
        max_retries=s.bank_max_retries,
        retry_on=frozenset({429, 502, 503, 504}),
        retry_on_timeout=True,
        backoff_base_s=0.2,
        backoff_cap_s=2.0,
        breaker_failures=s.breaker_failures,
        breaker_reset_s=s.breaker_reset_s,
    )


class BankClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._api_key = settings.bank_stub_api_key
        self._user_agent = f"payrun-payout-worker/{settings.version}"
        self._http = ResilientClient(bank_policy(settings), transport=transport, clock=clock)

    @property
    def http(self) -> ResilientClient:
        return self._http

    def _headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self._api_key.get_secret_value(),
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }

    async def create_transfer(self, key: str, body: TransferRequest) -> BankCallResult:
        try:
            resp = await self._http.request_raw(
                "POST",
                "/v1/transfers",
                operation="create_transfer",
                json=body.model_dump(mode="json"),
                headers=self._headers(),
                idempotency_key=key,
            )
        except ProviderError as exc:
            return BankCallResult(None, exc)
        return BankCallResult(resp)

    async def get_transfer(self, key: str) -> BankCallResult:
        try:
            resp = await self._http.request_raw(
                "GET",
                f"/v1/transfers/{quote(key, safe='')}",
                operation="get_transfer",
                headers=self._headers(),
            )
        except ProviderError as exc:
            return BankCallResult(None, exc)
        return BankCallResult(resp)

    async def aclose(self) -> None:
        await self._http.aclose()

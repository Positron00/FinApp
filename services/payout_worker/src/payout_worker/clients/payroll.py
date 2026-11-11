"""``PayrollClient``: instruction and result over pycommon's ``ResilientClient`` (design §3.4;
PAY-405, PAY-411, R2, R10, C26, C30, C34, C46).

A fresh JWT per HTTP try (``auth_headers``) with ``cid`` = the event's company. Both calls are
idempotent (a GET and a POST with an ``Idempotency-Key``), so connect errors and read timeouts are
retried too (R10). The final response is mapped here, not by pycommon.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Final
from uuid import UUID

import httpx
from pycommon import Clock
from pycommon.http import ProviderError, ProviderPolicy, ProviderUnavailable, ResilientClient
from pydantic import ValidationError

from payout_worker.errors import (
    AttemptNotCurrent,
    PayoutNotFound,
    PayrollRejected,
    PayrollUnavailable,
    safe_code,
)
from payout_worker.jwt_signer import WorkerJwtSigner
from payout_worker.models import Instruction, ResultRequest, ResultResponse
from payout_worker.settings import Settings

__all__ = ["PayrollClient", "payroll_policy"]

PROVIDER: Final = "payroll"
_cid: ContextVar[UUID | None] = ContextVar("payout_worker_payroll_cid", default=None)


def payroll_policy(s: Settings) -> ProviderPolicy:
    return ProviderPolicy(
        name=PROVIDER,
        base_url=s.payout_payroll_base_url,
        connect_timeout=s.payout_payroll_connect_timeout_s,
        read_timeout=s.payout_payroll_read_timeout_s,
        total_deadline=s.payout_payroll_call_deadline_s,
        max_retries=s.payout_payroll_max_retries,
        retry_on=frozenset({429, 502, 503, 504}),
        retry_on_timeout=True,
        backoff_base_s=0.1,
        backoff_cap_s=1.0,
        breaker_failures=s.breaker_failures,
        breaker_reset_s=s.breaker_reset_s,
    )


def _error_code(resp: httpx.Response) -> str | None:
    try:
        body = json.loads(resp.content)
    except ValueError:
        return None
    err = body.get("error") if isinstance(body, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    return code if isinstance(code, str) else None


class PayrollClient:
    def __init__(
        self,
        settings: Settings,
        signer: WorkerJwtSigner,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._signer = signer
        self._http = ResilientClient(
            payroll_policy(settings), auth_headers=self._auth, transport=transport, clock=clock
        )

    @property
    def http(self) -> ResilientClient:
        return self._http

    async def _auth(self) -> Mapping[str, str]:
        cid = _cid.get()
        if cid is None:  # pragma: no cover  (every call sets it)
            raise RuntimeError("no company for the worker token")
        return {"Authorization": f"Bearer {self._signer.mint(cid)}"}

    async def _call(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        company_id: UUID,
        request_id: str | None,
        params: Mapping[str, str] | None = None,
        json_body: object = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if request_id is not None:
            headers["X-Request-Id"] = request_id
        token = _cid.set(company_id)
        try:
            return await self._http.request_raw(
                method,
                path,
                operation=operation,
                params=params,
                json=json_body,
                headers=headers,
                idempotency_key=idempotency_key,
            )
        except ProviderUnavailable as exc:
            code = "breaker_open" if exc.reason == "breaker_open" else "payroll_unavailable"
            raise PayrollUnavailable(code) from exc
        except ProviderError as exc:
            raise PayrollUnavailable("payroll_unavailable") from exc
        finally:
            _cid.reset(token)

    @staticmethod
    def _failure(resp: httpx.Response, code: str | None) -> Exception:
        status = resp.status_code
        if status == 429 or status >= 500:
            return PayrollUnavailable("payroll_unavailable")
        return PayrollRejected(safe_code(code, f"http_{status}"))

    async def get_instruction(
        self, payout_id: UUID, attempt: int, company_id: UUID, *, request_id: str | None = None
    ) -> Instruction:
        """Raises AttemptNotCurrent | PayoutNotFound | PayrollUnavailable | PayrollRejected."""
        resp = await self._call(
            "GET",
            f"/v1/internal/payouts/{payout_id}/instruction",
            operation="get_instruction",
            company_id=company_id,
            request_id=request_id,
            params={"attempt": str(attempt)},
        )
        if 200 <= resp.status_code < 300:
            try:
                return Instruction.model_validate_json(resp.content)
            except ValidationError:
                raise PayrollRejected("malformed_response") from None
        code = _error_code(resp)
        if resp.status_code == 409 and code == "attempt_not_current":
            raise AttemptNotCurrent()
        if resp.status_code == 404 and code == "payout_not_found":
            raise PayoutNotFound()
        raise self._failure(resp, code)

    async def post_result(
        self,
        payout_id: UUID,
        company_id: UUID,
        body: ResultRequest,
        idempotency_key: str,
        *,
        request_id: str | None = None,
    ) -> ResultResponse:
        """Raises AttemptNotCurrent | PayrollUnavailable | PayrollRejected."""
        resp = await self._call(
            "POST",
            f"/v1/internal/payouts/{payout_id}/result",
            operation="post_result",
            company_id=company_id,
            request_id=request_id,
            json_body=body.model_dump(mode="json"),
            idempotency_key=idempotency_key,
        )
        if 200 <= resp.status_code < 300:
            try:
                return ResultResponse.model_validate_json(resp.content)
            except ValidationError:
                raise PayrollRejected("malformed_response") from None
        code = _error_code(resp)
        if resp.status_code == 409 and code == "attempt_not_current":
            raise AttemptNotCurrent()
        raise self._failure(resp, code)

    async def aclose(self) -> None:
        await self._http.aclose()

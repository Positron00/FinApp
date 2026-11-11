"""``POST /v1/transfers`` and ``GET /v1/transfers/{key}`` (design §3.1, §3.2, §5.3; PAY-412).

The POST body is read raw and validated in the handler, after the replay lookup, so a repeated
key returns the stored transfer whatever the body (R6). HTTP mapping only; the outcome rules
live in ``bank_stub.domain.outcome``.
"""

# ruff: noqa: N818  (the design's error names)

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Final, cast

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from opentelemetry.trace import Span, SpanKind, StatusCode
from opentelemetry.trace import Status as SpanStatus
from pycommon.db import new_id
from pycommon.errors import AppError, ErrorBody, ValidationFailed
from pycommon.logging import get_logger, log_event
from pycommon.otel import tracer
from pydantic import ValidationError

from bank_stub.api.auth import require_api_key
from bank_stub.db.repo import NewTransfer, TransferRow
from bank_stub.domain.outcome import Reason, Status, bank_reference_for, decide_outcome
from bank_stub.runtime import Runtime, get_runtime
from bank_stub.schemas import (
    IDEMPOTENCY_KEY_PATTERN,
    IDEMPOTENCY_KEY_RE,
    TransferRequest,
    TransferResponse,
)

__all__ = ["IdempotencyKeyRequired", "TransferNotFound", "router"]

_log: Final = get_logger("bank_stub.transfers")
SPAN_NAME: Final = "bank_stub.transfer"


class IdempotencyKeyRequired(AppError):
    status = 400
    code = "idempotency_key_required"
    default_message = "The Idempotency-Key header is required."


class TransferNotFound(AppError):
    status = 404
    code = "transfer_not_found"
    default_message = "No transfer has this idempotency key."


def _errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {s: {"model": ErrorBody} for s in sorted({401, 422, 500, 503, *statuses})}


router = APIRouter(prefix="/v1/transfers", tags=["transfers"])
RT = Annotated[Runtime, Depends(get_runtime)]
ApiKey = Depends(require_api_key)

_REQUEST_BODY: Final[dict[str, Any]] = {
    "requestBody": {
        "required": True,
        "content": {"application/json": {"schema": TransferRequest.model_json_schema()}},
    }
}


def _response(row: TransferRow, status_code: int) -> JSONResponse:
    body = TransferResponse(
        transfer_id=row.id,
        status=cast(Status, row.status),  # the table's CHECK constraints guarantee these
        reason=cast(Reason | None, row.reason),
        bank_reference=row.bank_reference,
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code)


def _key_error(field: str) -> ValidationFailed:
    return ValidationFailed(details=[{"field": field, "issue": "pattern"}])


def _body_error(exc: ValidationError) -> tuple[ValidationFailed, list[str]]:
    details = [
        {
            "field": ".".join(["body", *(str(p) for p in e.get("loc", ()))]),
            "issue": str(e.get("type", "")),
        }
        for e in exc.errors(include_input=False, include_context=False, include_url=False)
    ]
    return ValidationFailed(details=details), [d["field"] for d in details]


@contextmanager
def _transfer_span(operation: str, key: str) -> Iterator[Span]:
    """``bank_stub.transfer`` (INTERNAL): status ERROR only for a 5xx (design §5.3)."""
    with tracer("bank_stub").start_as_current_span(
        SPAN_NAME,
        kind=SpanKind.INTERNAL,
        attributes={"payrun.bank_operation": operation, "payrun.idempotency_key": key},
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except AppError:  # 4xx only: no AppError with a 5xx status is raised in the span
            raise
        except Exception as exc:
            span.set_status(SpanStatus(StatusCode.ERROR, type(exc).__name__))
            raise


def _replayed(span: Span, row: TransferRow) -> JSONResponse:
    span.set_attribute("payrun.transfer_id", str(row.id))
    span.set_attribute("payrun.bank_result", "replayed")
    log_event(
        _log,
        logging.INFO,
        "bank_stub.transfer.replayed",
        transfer_id=str(row.id),
        idempotency_key=row.idempotency_key,
        status=row.status,
    )
    return _response(row, 200)


@router.post(
    "",
    dependencies=[ApiKey],
    status_code=201,
    response_model=TransferResponse,
    responses={
        200: {"model": TransferResponse, "description": "Repeated key: the stored transfer"},
        **_errors(400, 413),
    },
    openapi_extra=_REQUEST_BODY,
)
async def create_transfer(
    request: Request,
    rt: RT,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            json_schema_extra={"pattern": IDEMPOTENCY_KEY_PATTERN, "maxLength": 255},
        ),
    ] = None,
) -> JSONResponse:
    if idempotency_key is None:
        raise IdempotencyKeyRequired()
    if not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
        raise _key_error("header.Idempotency-Key")
    key = idempotency_key
    await rt.sleep(rt.settings.bank_stub_latency_ms / 1000)
    with _transfer_span("create", key) as span:
        row = await rt.repo.get_by_key(key)
        if row is not None:
            rt.metrics.transfer("replayed")
            return _replayed(span, row)
        raw = await request.body()
        try:
            body = TransferRequest.model_validate_json(raw)
        except ValidationError as exc:
            err, fields = _body_error(exc)
            span.add_event(
                "exception", {"exception.type": "ValidationFailed", "payrun.fields": fields}
            )
            raise err from None
        status, reason = decide_outcome(key, body.account, rt.settings.bank_stub_failure_rate)
        row, created = await rt.repo.insert_or_get(
            NewTransfer(
                id=new_id(),
                idempotency_key=key,
                amount_cents=body.amount_cents,
                currency=body.currency,
                account_last4=body.account[-4:],
                status=status,
                reason=reason,
                bank_reference=bank_reference_for(key) if status == "settled" else None,
            )
        )
        if not created:
            rt.metrics.transfer("replayed")
            return _replayed(span, row)
        span.set_attribute("payrun.transfer_id", str(row.id))
        span.set_attribute("payrun.bank_result", row.status)
        rt.metrics.transfer("settled" if row.status == "settled" else "rejected")
        log_event(
            _log,
            logging.INFO,
            "bank_stub.transfer.created",
            transfer_id=str(row.id),
            idempotency_key=key,
            status=row.status,
            reason=row.reason,
        )
        return _response(row, 201)


@router.get(
    "/{key}",
    dependencies=[ApiKey],
    response_model=TransferResponse,
    responses=_errors(404),
)
async def get_transfer(key: str, rt: RT) -> JSONResponse:
    if not IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise _key_error("path.key")
    await rt.sleep(rt.settings.bank_stub_latency_ms / 1000)
    with _transfer_span("get", key) as span:
        row = await rt.repo.get_by_key(key)
        if row is None:
            span.set_attribute("payrun.bank_result", "not_found")
            log_event(_log, logging.INFO, "bank_stub.transfer.not_found", idempotency_key=key)
            raise TransferNotFound()
        span.set_attribute("payrun.transfer_id", str(row.id))
        span.set_attribute("payrun.bank_result", row.status)
        return _response(row, 200)

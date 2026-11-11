"""The error format and exception handlers (platform LLD §3.4; PAY-602, C14, C56).

Every error body is ``{"error": {"code", "message", "request_id", "details?"}}``. No traceback or
exception text ever reaches a response.
"""

# ruff: noqa: N818  (exception names are the platform LLD contract)

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Final, Literal, cast

import asyncpg  # type: ignore[import-untyped]
import redis.exceptions
import sqlalchemy.exc
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import trace
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from pycommon.logging import get_logger, log_event, request_id_var

__all__ = [
    "AppError",
    "BadRequest",
    "Conflict",
    "DbTimeout",
    "DbUnavailable",
    "ErrorBody",
    "ErrorDetail",
    "Forbidden",
    "NotFound",
    "PayloadTooLarge",
    "RateLimited",
    "ServiceUnavailable",
    "Unauthorized",
    "ValidationFailed",
    "db_error_kind",
    "error_response",
    "install_handlers",
]

_log = get_logger("pycommon.errors")

INTERNAL_MESSAGE: Final = "Something went wrong."


class AppError(Exception):
    status: ClassVar[int] = 500
    code: ClassVar[str] = "internal_error"
    default_message: ClassVar[str] = INTERNAL_MESSAGE

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.error_code = code or self.code
        self.details = details
        self.headers: dict[str, str] = dict(headers or {})
        super().__init__(self.message)


class BadRequest(AppError):
    status = 400
    code = "bad_request"
    default_message = "The request is not valid."


class Unauthorized(AppError):
    status = 401
    code = "invalid_token"
    default_message = "Authentication is required."


class Forbidden(AppError):
    status = 403
    code = "forbidden_role"
    default_message = "You don't have permission to do this."


class NotFound(AppError):
    status = 404
    code = "not_found"
    default_message = "Not found."


class Conflict(AppError):
    status = 409
    code = "conflict"
    default_message = "The request conflicts with the current state."


class PayloadTooLarge(AppError):
    status = 413
    code = "payload_too_large"
    default_message = "The request body is too large."


class ValidationFailed(AppError):
    status = 422
    code = "validation_error"
    default_message = "The request failed validation."


class RateLimited(AppError):
    status = 429
    code = "rate_limited"
    default_message = "Too many requests. Try again later."

    def __init__(
        self,
        message: str | None = None,
        *,
        retry_after_s: int = 1,
        code: str | None = None,
        details: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.retry_after_s = max(1, int(retry_after_s))
        merged = {"Retry-After": str(self.retry_after_s), **(headers or {})}
        super().__init__(message, code=code, details=details, headers=merged)


class ServiceUnavailable(AppError):
    status = 503
    code = "service_unavailable"
    default_message = "The service is temporarily unavailable."


class DbTimeout(ServiceUnavailable):
    code = "db_timeout"
    default_message = "The database did not respond in time."


class DbUnavailable(ServiceUnavailable):
    code = "db_unavailable"
    default_message = "The database is unavailable."


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str | None
    details: Any = None


class ErrorBody(BaseModel):
    error: ErrorDetail


def error_response(
    status: int,
    code: str,
    message: str,
    *,
    details: Any = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message, "request_id": request_id_var.get()}
    if details is not None:
        error["details"] = details
    return JSONResponse({"error": error}, status_code=status, headers=dict(headers or {}))


_STATUS_CODES: Final[dict[int, tuple[str, str]]] = {
    400: ("bad_request", BadRequest.default_message),
    401: ("invalid_token", Unauthorized.default_message),
    403: ("forbidden_role", Forbidden.default_message),
    404: ("not_found", NotFound.default_message),
    405: ("method_not_allowed", "Method not allowed."),
    409: ("conflict", Conflict.default_message),
    413: ("payload_too_large", PayloadTooLarge.default_message),
    415: ("unsupported_media_type", "Unsupported media type."),
    422: ("validation_error", ValidationFailed.default_message),
    429: ("rate_limited", RateLimited.default_message),
    503: ("service_unavailable", ServiceUnavailable.default_message),
}


def _causes(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    stack: list[BaseException] = [exc]
    seen: set[int] = set()
    while stack:
        e = stack.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        out.append(e)
        orig = getattr(e, "orig", None)
        stack.extend(
            nxt for nxt in (orig, e.__cause__, e.__context__) if isinstance(nxt, BaseException)
        )
    return out


_TIMEOUT_SQLSTATES: Final = frozenset({"57014"})  # query_canceled (statement_timeout)
_UNAVAILABLE_SQLSTATES_PREFIX: Final = ("08", "57P")  # connection exceptions, admin shutdown


def db_error_kind(exc: BaseException) -> Literal["timeout", "unavailable"] | None:
    """Classify a DB driver error: ``timeout`` (503 db_timeout), ``unavailable`` or ``None``."""
    chain = _causes(exc)
    unavailable = False
    for e in chain:
        sqlstate = getattr(e, "sqlstate", None) or getattr(e, "pgcode", None)
        if (
            isinstance(e, asyncpg.QueryCanceledError | sqlalchemy.exc.TimeoutError | TimeoutError)
            or sqlstate in _TIMEOUT_SQLSTATES
        ):
            return "timeout"
        if isinstance(sqlstate, str) and sqlstate.startswith(_UNAVAILABLE_SQLSTATES_PREFIX):
            unavailable = True
        if isinstance(
            e,
            asyncpg.ConnectionDoesNotExistError
            | asyncpg.CannotConnectNowError
            | asyncpg.InterfaceError
            | sqlalchemy.exc.OperationalError
            | sqlalchemy.exc.InterfaceError
            | sqlalchemy.exc.DisconnectionError
            | OSError,
        ) or (isinstance(e, sqlalchemy.exc.DBAPIError) and e.connection_invalidated):
            unavailable = True
    return "unavailable" if unavailable else None


def _operation(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None) or "unmatched"
    return f"{request.method} {path}"


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    err = cast(AppError, exc)
    level = logging.WARNING if err.status >= 500 else logging.INFO
    log_event(_log, level, "http.error", code=err.error_code, status=err.status)
    return error_response(
        err.status, err.error_code, err.message, details=err.details, headers=err.headers
    )


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    errors = cast(RequestValidationError, exc).errors()
    details = [
        {"field": ".".join(str(p) for p in err.get("loc", ())), "issue": str(err.get("type", ""))}
        for err in errors
    ]
    log_event(_log, logging.INFO, "http.error", code="validation_error", status=422)
    return error_response(
        422, "validation_error", ValidationFailed.default_message, details=details
    )


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    err = cast(StarletteHTTPException, exc)
    code, message = _STATUS_CODES.get(err.status_code, ("http_error", "The request failed."))
    if err.status_code >= 500 and err.status_code not in _STATUS_CODES:
        code, message = "internal_error", INTERNAL_MESSAGE
    return error_response(err.status_code, code, message, headers=err.headers)


def db_error_response(request: Request, exc: BaseException) -> JSONResponse | None:
    kind = db_error_kind(exc)
    if kind == "timeout":
        log_event(_log, logging.WARNING, "db.timeout", operation=_operation(request))
        return error_response(503, DbTimeout.code, DbTimeout.default_message)
    if kind == "unavailable":
        log_event(_log, logging.WARNING, "db.unavailable", operation=_operation(request))
        return error_response(503, DbUnavailable.code, DbUnavailable.default_message)
    return None


async def _db_handler(request: Request, exc: Exception) -> JSONResponse:
    return db_error_response(request, exc) or unhandled_response(exc)


async def _redis_handler(request: Request, exc: Exception) -> JSONResponse:
    log_event(_log, logging.WARNING, "redis.unavailable", operation=_operation(request))
    return error_response(503, ServiceUnavailable.code, ServiceUnavailable.default_message)


def unhandled_response(exc: BaseException) -> JSONResponse:
    """Log ``http.unhandled`` with the traceback, record it on the span, and build the 500 body."""
    span = trace.get_current_span()
    if span.is_recording():
        span.record_exception(exc)
        span.set_status(trace.Status(trace.StatusCode.ERROR, type(exc).__name__))
    log_event(
        _log,
        logging.ERROR,
        "http.unhandled",
        error_type=type(exc).__name__,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return error_response(500, "internal_error", INTERNAL_MESSAGE)


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    return db_error_response(request, exc) or unhandled_response(exc)


def install_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    for db_exc in (sqlalchemy.exc.SQLAlchemyError, asyncpg.PostgresError, asyncpg.InterfaceError):
        app.add_exception_handler(db_exc, _db_handler)
    app.add_exception_handler(OSError, _db_handler)
    app.add_exception_handler(redis.exceptions.ConnectionError, _redis_handler)
    app.add_exception_handler(redis.exceptions.TimeoutError, _redis_handler)
    app.add_exception_handler(Exception, _unhandled_handler)

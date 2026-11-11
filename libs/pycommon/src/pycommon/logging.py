"""JSON logging with redaction (platform LLD §3.2; PAY-NFR-07).

Callers log with :func:`log_event` and never interpolate values into the message: the message is
not redacted, the structured ``fields`` are.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import sys
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from opentelemetry import trace
from pydantic import SecretStr

if TYPE_CHECKING:
    from opentelemetry.sdk.resources import Resource

    from pycommon.settings import BaseServiceSettings

__all__ = [
    "REDACTED",
    "REDACT_KEYS",
    "JsonFormatter",
    "RedactingFilter",
    "get_logger",
    "log_event",
    "redact",
    "request_id_var",
    "setup_logging",
    "user_text",
]

REDACT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "token",
        "authorization",
        "cookie",
        "account",
        "routing",
        "api_key",  # PAY-NFR-07
        "secret",
        "private_key",
        "pem",
        "set-cookie",
        "x-api-key",
        "salary",
        "amount",
        "dsn",
    }
)
REDACTED: Final = "[REDACTED]"
MAX_DEPTH: Final = 8

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_JWT_RE: Final = re.compile(r"^eyJ[\w-]+\.[\w-]+\.[\w-]+$")
_BEARER_RE: Final = re.compile(r"^\s*bearer\s+\S+", re.IGNORECASE)
_PEM_RE: Final = re.compile(r"-----BEGIN [A-Z0-9 ]+-----")

# LogRecord attributes that are not caller-supplied extras.
_RECORD_ATTRS: Final[frozenset[str]] = frozenset(
    vars(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None))
) | {"message", "asctime", "taskName"}

_HANDLER_TAG: Final = "_pycommon_handler"


def _key_is_sensitive(key: object) -> bool:
    k = str(key).lower()
    return any(word in k for word in REDACT_KEYS)


def _value_is_sensitive(value: str) -> bool:
    v = value.strip()
    return bool(_JWT_RE.match(v) or _BEARER_RE.match(v) or _PEM_RE.search(v))


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Mask deny-listed keys (substring, any case) and JWT/Bearer/PEM values, recursively."""
    if isinstance(value, SecretStr):
        return REDACTED
    if isinstance(value, str):
        return REDACTED if _value_is_sensitive(value) else value
    if isinstance(value, Mapping | list | tuple) and _depth >= MAX_DEPTH:
        return REDACTED
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if _key_is_sensitive(k) else redact(v, _depth=_depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v, _depth=_depth + 1) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line: fixed keys, then the redacted fields, then ``exc``."""

    def __init__(self, *, service: str, env: str, version: str) -> None:
        super().__init__()
        self._service = service
        self._env = env
        self._version = version

    def format(self, record: logging.LogRecord) -> str:
        ctx = trace.get_current_span().get_span_context()
        valid = ctx.is_valid
        ts = datetime.fromtimestamp(record.created, UTC)
        doc: dict[str, Any] = {
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z",
            "level": record.levelname.lower(),
            "service": self._service,
            "env": self._env,
            "version": self._version,
            "logger": record.name,
            "event": getattr(record, "event", None),
            "msg": record.getMessage(),
            "trace_id": f"{ctx.trace_id:032x}" if valid else None,
            "span_id": f"{ctx.span_id:016x}" if valid else None,
            "request_id": request_id_var.get(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            for k, v in redact(fields).items():
                if k not in doc:
                    doc[str(k)] = v
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str)


def _attr_value(value: Any) -> Any:
    if isinstance(value, str | bool | int | float):
        return value
    if value is None:
        return ""
    return json.dumps(value, default=str)


class RedactingFilter(logging.Filter):
    """Gives the OTel handler a copy of the record whose extras are redacted and flattened.

    The OTel ``LoggingHandler`` exports every non-standard record attribute; this filter makes sure
    those attributes went through :func:`redact` (the same deny-list as the JSON lines).
    """

    def filter(self, record: logging.LogRecord) -> logging.LogRecord:
        clone = copy.copy(record)
        extras = {k: v for k, v in vars(record).items() if k not in _RECORD_ATTRS}
        for k in extras:
            delattr(clone, k)
        fields = extras.pop("fields", None)
        flat: dict[str, Any] = {}
        if isinstance(fields, Mapping):
            flat.update({str(k): v for k, v in fields.items()})
        flat.update(extras)
        for k, v in redact(flat).items():
            setattr(clone, k, _attr_value(v))
        return clone


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, event: str, msg: str = "", **fields: Any) -> None:
    """Log ``event`` with structured ``fields``; ``exc_info=…`` is passed through to logging."""
    exc_info = fields.pop("exc_info", None)
    logger.log(
        level,
        msg or event,
        extra={"event": event, "fields": fields},
        exc_info=exc_info,
        stacklevel=2,
    )


def user_text(value: str, settings: BaseServiceSettings) -> str | int:
    """User-entered text for logs: the text itself in dev, else only its length (PAY-NFR-07)."""
    return value if settings.env == "dev" else len(value)


def setup_logging(settings: BaseServiceSettings, resource: Resource | None = None) -> None:
    """JSON lines on stdout, plus the OTel ``LoggingHandler`` (behind a redactor) when ``resource``
    is given. Idempotent: earlier pycommon handlers are replaced; an installed OTel handler is kept
    when this is called again without a resource.
    """
    root = logging.getLogger()
    keep_otel = resource is None
    for h in list(root.handlers):
        tag = getattr(h, _HANDLER_TAG, None)
        if tag == "stdout" or (tag == "otel" and not keep_otel):
            root.removeHandler(h)
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(
        JsonFormatter(
            service=settings.service_name, env=str(settings.env), version=settings.version
        )
    )
    setattr(stdout, _HANDLER_TAG, "stdout")
    root.addHandler(stdout)
    if resource is not None:
        from opentelemetry._logs import get_logger_provider  # noqa: PLC0415
        from opentelemetry.instrumentation.logging.handler import LoggingHandler  # noqa: PLC0415

        otel_handler = LoggingHandler(logger_provider=get_logger_provider())
        otel_handler.addFilter(RedactingFilter())
        setattr(otel_handler, _HANDLER_TAG, "otel")
        root.addHandler(otel_handler)
    root.setLevel(settings.log_level)
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

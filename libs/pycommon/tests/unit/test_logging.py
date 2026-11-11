"""JSON logging and redaction (platform LLD §3.2; PAY-NFR-07, NFR-09)."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from pycommon.logging import (
    REDACT_KEYS,
    REDACTED,
    JsonFormatter,
    RedactingFilter,
    get_logger,
    log_event,
    redact,
    request_id_var,
    setup_logging,
    user_text,
)
from pycommon.otel import build_resource
from pycommon.testing import JsonLogCapture, OtelMemory
from pydantic import SecretStr

from tests.helpers import make_settings

REQUIRED_KEYS = {
    "ts",
    "level",
    "service",
    "env",
    "version",
    "logger",
    "event",
    "msg",
    "trace_id",
    "span_id",
    "request_id",
}
# Fake credentials are assembled at runtime so no literal in the repo looks like a real
# token or key (testing.md §9; keeps gitleaks and GitHub secret scanning quiet).
_B64 = "eyJ"  # base64 of '{"'
JWT = ".".join([_B64 + "hbGciOiJFZERTQSJ9", _B64 + "zdWIiOiJ4In0", "c2lnbmF0dXJl"])
_KEY_LABEL = " ".join(["PRIVATE", "KEY"])
PEM = f"-----BEGIN {_KEY_LABEL}-----\nMC4CAQAwBQYDK2VwBCIEI\n-----END {_KEY_LABEL}-----"


@pytest.fixture
def restore_root() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    access = logging.getLogger("uvicorn.access")
    access_state = (list(access.handlers), access.propagate, access.disabled)
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    access.handlers[:], access.propagate, access.disabled = access_state


def _format(record_fields: dict[str, Any], **kw: Any) -> dict[str, Any]:
    fmt = JsonFormatter(service="payroll", env="dev", version="1.2.3")
    record = logging.LogRecord("pycommon.x", logging.INFO, __file__, 1, "hello", None, None)
    record.event = "thing.happened"
    record.fields = record_fields
    for k, v in kw.items():
        setattr(record, k, v)
    out: dict[str, Any] = json.loads(fmt.format(record))
    return out


def test_json_line_has_every_required_field() -> None:
    """NFR-09: one JSON object per line with the fixed keys, then fields."""
    token = request_id_var.set("req-123")
    try:
        doc = _format({"count": 3})
    finally:
        request_id_var.reset(token)
    assert set(doc) >= REQUIRED_KEYS
    assert doc["service"] == "payroll"
    assert doc["env"] == "dev"
    assert doc["version"] == "1.2.3"
    assert doc["event"] == "thing.happened"
    assert doc["msg"] == "hello"
    assert doc["request_id"] == "req-123"
    assert doc["count"] == 3
    assert doc["level"] == "info"
    assert doc["ts"].endswith("Z")
    assert doc["trace_id"] is None
    assert doc["span_id"] is None


def test_fields_never_override_fixed_keys() -> None:
    """NFR-09: a field named like a fixed key can't spoof it."""
    doc = _format({"service": "spoofed", "level": "debug"})
    assert doc["service"] == "payroll"
    assert doc["level"] == "info"


def test_trace_and_span_ids_inside_a_span(otel_memory: OtelMemory) -> None:
    """NFR-09: every log line carries trace_id/span_id of the active span."""
    tracer = otel_memory.tracer_provider.get_tracer("t")
    with tracer.start_as_current_span("work") as span:
        doc = _format({})
        ctx = span.get_span_context()
    assert doc["trace_id"] == f"{ctx.trace_id:032x}"
    assert doc["span_id"] == f"{ctx.span_id:016x}"


@pytest.mark.parametrize(
    "key",
    ["password", "token", "authorization", "cookie", "account", "routing", "api_key"],
)
def test_redact_masks_each_nfr07_key(key: str) -> None:
    """PAY-NFR-07: the seven required deny-list keys are masked."""
    assert redact({key: "value"}) == {key: REDACTED}
    assert redact({key.upper(): "value"}) == {key.upper(): REDACTED}


def test_redact_masks_extra_keys_by_substring_case_insensitively() -> None:
    """PAY-NFR-07: substring match, any case, the extra keys too."""
    data = {
        "bank_account": "123456789",
        "Reset_Token": "abc",
        "annual_salary_cents": 9_000_000,
        "X-API-Key": "k",
        "Set-Cookie": "sid=1",
        "private_key": "p",
        "amount_cents": 100,
        "dsn": "postgres://",
        "client_secret": "s",
        "user_id": "u-1",
    }
    out = redact(data)
    assert {k for k, v in out.items() if v == REDACTED} == set(data) - {"user_id"}
    assert out["user_id"] == "u-1"
    assert {"password", "token", "api_key", "salary", "amount", "dsn"} <= REDACT_KEYS


def test_redact_recurses_into_dicts_lists_and_tuples() -> None:
    """PAY-NFR-07: nested containers are redacted."""
    data = {"outer": [{"password": "x", "ok": 1}, ("keep", {"routing": "021"})]}
    assert redact(data) == {
        "outer": [{"password": REDACTED, "ok": 1}, ["keep", {"routing": REDACTED}]]
    }


@pytest.mark.parametrize("value", [JWT, f"Bearer {JWT}", "bearer abc.def", PEM])
def test_redact_masks_secret_looking_values_under_innocent_keys(value: str) -> None:
    """PAY-NFR-07: JWT, Bearer and PEM values are masked whatever their key."""
    assert redact({"note": value}) == {"note": REDACTED}
    assert redact([value]) == [REDACTED]


def test_redact_secretstr_and_depth_cap() -> None:
    """PAY-NFR-07: SecretStr renders as REDACTED; containers deeper than 8 levels are masked."""
    assert redact({"x": SecretStr("hidden")}) == {"x": REDACTED}
    deep: Any = "leaf"
    for _ in range(12):
        deep = {"n": deep}
    out = redact(deep)
    for _ in range(8):
        out = out["n"]
    assert out == REDACTED
    assert redact(42) == 42
    assert redact("plain text") == "plain text"


def test_user_text_is_length_outside_dev() -> None:
    """PAY-NFR-07: user-entered text outside dev is logged as its length only."""
    assert user_text("my note", make_settings(env="dev")) == "my note"
    assert user_text("my note", make_settings(env="uat")) == 7
    assert user_text("my note", make_settings(env="prod")) == 7


def test_log_event_passes_fields_and_traceback(caplog_json: JsonLogCapture) -> None:
    """NFR-09: log_event sets event and fields; exc_info adds the traceback as ``exc``."""
    log = get_logger("pycommon.test")
    try:
        raise ValueError("boom")
    except ValueError as exc:
        log_event(log, logging.ERROR, "x.failed", reason="r", exc_info=exc)
    log_event(log, logging.INFO, "x.ok", "custom message", password="pw")
    failed, ok = caplog_json.records()
    assert failed["event"] == "x.failed"
    assert failed["reason"] == "r"
    assert "ValueError: boom" in failed["exc"]
    assert ok["msg"] == "custom message"
    assert ok["password"] == REDACTED


def test_setup_logging_installs_json_stdout_and_silences_access_log(
    restore_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NFR-07: JSON on stdout; uvicorn.access silenced; httpx/httpcore at WARNING; idempotent."""
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    s = make_settings(log_level="WARNING", version="9.9.9")
    setup_logging(s)
    setup_logging(s)
    root = logging.getLogger()
    tagged = [h for h in root.handlers if getattr(h, "_pycommon_handler", None) == "stdout"]
    assert len(tagged) == 1
    assert root.level == logging.WARNING
    access = logging.getLogger("uvicorn.access")
    assert access.disabled
    assert not access.propagate
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    log_event(get_logger("svc"), logging.WARNING, "svc.warned", token="t0k3n")
    access.warning("GET /secret?token=abc")
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert [line["event"] for line in lines] == ["svc.warned"]
    assert lines[0]["version"] == "9.9.9"
    assert lines[0]["token"] == REDACTED


def test_otlp_handler_gets_redacted_attributes(
    restore_root: None, otel_memory: OtelMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAY-NFR-07: the OTel LoggingHandler exports the same redacted fields as stdout."""
    monkeypatch.setattr("sys.stdout", io.StringIO())
    s = make_settings()
    setup_logging(s, build_resource(s))
    setup_logging(s)  # a later call without a resource keeps the OTel handler
    root = logging.getLogger()
    assert [getattr(h, "_pycommon_handler", None) for h in root.handlers].count("otel") == 1
    log_event(
        get_logger("svc"),
        logging.INFO,
        "svc.done",
        password="hunter2-pw",
        nested={"api_key": "k-123", "ok": True},
        auth_header=f"Bearer {JWT}",
        count=2,
    )
    otel_memory.logger_provider.force_flush()
    records = otel_memory.log_records()
    assert records
    attrs = dict(records[-1].log_record.attributes or {})
    assert attrs["password"] == REDACTED
    assert attrs["auth_header"] == REDACTED
    assert attrs["count"] == 2
    assert json.loads(str(attrs["nested"])) == {"api_key": REDACTED, "ok": True}
    assert attrs["event"] == "svc.done"
    dumped = json.dumps({k: str(v) for k, v in attrs.items()})
    assert "hunter2-pw" not in dumped
    assert "k-123" not in dumped
    assert JWT not in dumped


def test_redacting_filter_returns_a_copy() -> None:
    """PAY-NFR-07: the stdout record is not altered by the OTel filter."""
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
    record.fields = {"password": "pw", "none": None}
    record.extra_thing = {"token": "t"}
    clone = RedactingFilter().filter(record)
    assert isinstance(clone, logging.LogRecord)
    assert clone is not record
    assert vars(record)["fields"] == {"password": "pw", "none": None}
    attrs = vars(clone)
    assert attrs["password"] == REDACTED
    assert attrs["none"] == ""
    assert json.loads(attrs["extra_thing"]) == {"token": REDACTED}
    assert not hasattr(clone, "fields")

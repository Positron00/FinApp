"""Metric names, labels and pre-initialisation (design §5.2; PAY-NFR-10, PAY-NFR-11, C21)."""

from __future__ import annotations

from typing import Any

import pytest
from payout_worker.main import build_app
from payout_worker.metrics import Metrics
from pycommon.testing import OtelMemory

from tests.helpers import make_metrics, make_settings

STREAM = {"stream": "stream:payout.requested", "group": "payout-worker"}


def _points(otel: OtelMemory, name: str) -> list[tuple[dict[str, Any], Any]]:
    return otel.metric_points(name)


def test_preinit_three_series_at_zero(otel_memory: OtelMemory) -> None:
    """C21: right after start-up queue_messages_total has ok, retry, dlq at 0."""
    make_metrics().preinit()
    points = _points(otel_memory, "queue_messages")
    assert sorted((a["outcome"], v) for a, v in points) == [("dlq", 0), ("ok", 0), ("retry", 0)]
    assert all({k: a[k] for k in STREAM} == STREAM for a, _ in points)


def test_names_and_labels(otel_memory: OtelMemory) -> None:
    """PAY-NFR-10: every §5.2 instrument with its exact labels; rejected reasons bucketed."""
    m: Metrics = make_metrics()
    m.message("ok")
    m.processed_in(0.2)
    m.bank_transfer("settled", "none")
    m.bank_transfer("rejected", "account_closed")
    m.bank_transfer("rejected", "weird_reason")
    m.bank_transfer("no_answer", "timeout")
    m.lag = 4
    m.dlq_length = 2
    names = otel_memory.metric_names()
    assert {
        "queue_messages",
        "queue_processing_seconds",
        "payouts_bank_transfers",
        "queue_consumer_lag",
        "queue_dlq_messages",
    } <= names
    bank = {
        (a["outcome"], a["reason"]): v for a, v in _points(otel_memory, "payouts_bank_transfers")
    }
    assert bank == {
        ("settled", "none"): 1,
        ("rejected", "account_closed"): 1,
        ("rejected", "other"): 1,
        ("no_answer", "timeout"): 1,
    }
    [(lag_attrs, lag)] = _points(otel_memory, "queue_consumer_lag")
    assert (lag_attrs, lag) == (STREAM, 4)
    [(dlq_attrs, dlq)] = _points(otel_memory, "queue_dlq_messages")
    assert (dlq_attrs, dlq) == ({"stream": "stream:payout.requested.dlq"}, 2)
    [(hist_attrs, _)] = _points(otel_memory, "queue_processing_seconds")
    assert hist_attrs == STREAM


def test_lag_unknown_yields_no_point(otel_memory: OtelMemory) -> None:
    """Design §3.3.8: no lag value yet → no observation."""
    make_metrics()
    assert _points(otel_memory, "queue_consumer_lag") == []


@pytest.mark.parametrize("consume", [False])
async def test_app_preinitialises_on_startup(otel_memory: OtelMemory, consume: bool) -> None:
    """C21: the app's on_startup pre-initialises the counters before any read."""
    app = build_app(make_settings(), instrument=False, consume=consume, maintenance=False)
    async with app.router.lifespan_context(app):
        points = _points(otel_memory, "queue_messages")
        assert sorted(a["outcome"] for a, _ in points) == ["dlq", "ok", "retry"]
        await app.state.worker.aclose()

"""Instrument names and units (design §7.2; PAY-NFR-10, PAY-NFR-11, PAY-407, R26, C63)."""

from __future__ import annotations

import pytest
from payroll import metrics as metrics_mod
from payroll.metrics import (
    GAUGES,
    GaugeStats,
    Metrics,
    PayrollGaugeSource,
    register_collection_gauges,
)
from pycommon.testing import JsonLogCapture, OtelMemory

from tests.helpers import make_settings

STATS = GaugeStats(
    runs_processing_oldest_age_s=12.5,
    payouts_stuck=2,
    attempts_needs_operator=1,
    outbox_unsent=3,
    outbox_oldest_age_s=4.0,
)
EXPECTED = {
    "payroll_runs_processing_oldest_age_seconds": ("s", 12.5),
    "payroll_payouts_stuck": ("{attempt}", 2.0),
    "payroll_attempts_needs_operator": ("{attempt}", 1.0),
    "outbox_unsent": ("{event}", 3.0),
    "outbox_oldest_age_seconds": ("s", 4.0),
}


def test_gauge_names_and_units_never_one() -> None:
    """PAY-NFR-11, C63, C65: the five gauges check_metrics.sh and the alerts read; no unit "1"."""
    assert {name: unit for name, unit, _, _ in GAUGES} == {k: v[0] for k, v in EXPECTED.items()}


def test_gauges_report_the_source_at_collection(
    otel_memory: OtelMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAY-NFR-11, R26: each collection calls the source; values come straight from its stats."""
    source = PayrollGaugeSource(make_settings())
    calls: list[int] = []

    def stats() -> GaugeStats:
        calls.append(1)
        return STATS

    monkeypatch.setattr(source, "stats", stats)
    register_collection_gauges(source)
    for name, (unit, value) in EXPECTED.items():
        assert otel_memory.metric_points(name) == [({}, value)]
        assert otel_memory.metric_unit(name) == unit
    assert calls
    source.close()
    assert otel_memory.metric_points("outbox_unsent") == []  # closed: no observation


def test_failed_query_yields_no_point(
    otel_memory: OtelMemory, monkeypatch: pytest.MonkeyPatch, caplog_json: JsonLogCapture
) -> None:
    """PAY-NFR-11, R26: a failing query logs metrics.gauge_query_failed and observes nothing."""
    source = PayrollGaugeSource(make_settings())

    def boom() -> GaugeStats:
        raise OSError("db down")

    monkeypatch.setattr(source, "stats", boom)
    register_collection_gauges(source)
    assert otel_memory.metric_points("payroll_payouts_stuck") == []
    assert caplog_json.events("metrics.gauge_query_failed")


def test_republished_counter_name(otel_memory: OtelMemory) -> None:
    """PAY-407: payroll_payouts_republished (Prometheus adds _total) with an annotation unit."""
    m = Metrics.create()
    m.republished.add(1)
    assert otel_memory.metric_points("payroll_payouts_republished") == [({}, 1)]
    assert otel_memory.metric_unit("payroll_payouts_republished") == "{event}"
    assert metrics_mod.STATS_REUSE_S > 0


def test_late_settlements_counter_is_pre_initialised(otel_memory: OtelMemory) -> None:
    """PAY-NFR-02, C21: payroll_late_settlements (Prometheus adds _total) exists at 0 from the
    start, so the PayoutLateSettlement alert's increase() sees the first late settlement."""
    m = Metrics.create()
    assert otel_memory.metric_points("payroll_late_settlements") == [({}, 0)]
    assert otel_memory.metric_unit("payroll_late_settlements") == "{attempt}"
    m.late_settlement()
    assert otel_memory.metric_points("payroll_late_settlements") == [({}, 1)]

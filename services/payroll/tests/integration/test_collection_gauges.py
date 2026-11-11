"""Collection-time gauges (design §7.2; PAY-NFR-11, PAY-402, PAY-409, R26, C18, C63; plan §12.1
"Outbox relay crash": unsent rows stay and ``outbox_oldest_age_seconds`` grows)."""

from __future__ import annotations

import socket
from typing import Any

import pytest
from payroll import metrics as metrics_mod
from payroll.metrics import PayrollGaugeSource, register_collection_gauges
from pycommon.testing import JsonLogCapture, OtelMemory

from tests.integration.conftest import Api, ApiFactory

pytestmark = pytest.mark.integration

NAMES = (
    "payroll_runs_processing_oldest_age_seconds",
    "payroll_payouts_stuck",
    "payroll_attempts_needs_operator",
    "outbox_unsent",
    "outbox_oldest_age_seconds",
)


def gauge(otel_memory: OtelMemory, name: str) -> float:
    [(_, value)] = otel_memory.metric_points(name)
    return float(value)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(autouse=True)
def _no_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics_mod, "STATS_REUSE_S", 0.0)


async def test_gauges_are_computed_at_collection(
    api_factory: ApiFactory, db: Any, otel_memory: OtelMemory
) -> None:
    """PAY-NFR-11, R26: all five are 0 when nothing matches; after approve (relay off) the
    outbox count and age, the processing age, stuck attempts and needs_operator follow the DB
    at each collection."""
    api = await api_factory(instrument=True)
    for name in NAMES:
        assert gauge(otel_memory, name) == 0, name
    run = await api.approved_run(3)
    assert gauge(otel_memory, "outbox_unsent") == 3
    await db.execute(
        "UPDATE payroll.outbox SET created_at = now() - interval '10 minutes'; "
        "UPDATE payroll.pay_runs SET processing_since = now() - interval '2 minutes'"
    )
    assert 595 < gauge(otel_memory, "outbox_oldest_age_seconds") < 700
    assert 115 < gauge(otel_memory, "payroll_runs_processing_oldest_age_seconds") < 200
    assert gauge(otel_memory, "payroll_payouts_stuck") == 0
    await db.execute(
        "UPDATE payroll.payout_attempts SET requested_at = now() - interval '11 minutes'"
    )
    assert gauge(otel_memory, "payroll_payouts_stuck") == 3
    [p, *_] = await api.payouts(run["id"])
    await api.result(p["id"], {"attempt": 1, "outcome": "poison"})
    assert gauge(otel_memory, "payroll_attempts_needs_operator") == 1
    assert gauge(otel_memory, "payroll_payouts_stuck") == 3  # needs_operator is still non-final
    await api.mark_outbox_sent(db)
    assert gauge(otel_memory, "outbox_unsent") == 0
    assert gauge(otel_memory, "outbox_oldest_age_seconds") == 0
    for q in await api.payouts(run["id"]):
        settled = {"attempt": 1, "outcome": "settled", "bank_reference": "B"}
        await api.result(q["id"], settled)
    assert gauge(otel_memory, "payroll_runs_processing_oldest_age_seconds") == 0
    assert gauge(otel_memory, "payroll_payouts_stuck") == 0
    assert otel_memory.metric_unit("payroll_payouts_stuck") == "{attempt}"
    assert otel_memory.metric_unit("outbox_oldest_age_seconds") == "s"


async def test_two_instances_report_the_same_fresh_values(api_factory: ApiFactory) -> None:
    """PAY-NFR-11, R26: every replica computes its own value from the shared DB at collection,
    so two app instances report the same, current numbers."""
    a = await api_factory()
    b = await api_factory()
    await a.approved_run(2)
    sources = [PayrollGaugeSource(x.rt.settings) for x in (a, b)]
    try:
        first, second = (s.stats() for s in sources)
        assert first.outbox_unsent == second.outbox_unsent == 2
        assert first.payouts_stuck == second.payouts_stuck == 0
    finally:
        for s in sources:
            s.close()


async def test_db_down_yields_no_observation(
    api: Api, otel_memory: OtelMemory, caplog_json: JsonLogCapture
) -> None:
    """PAY-NFR-11, R26: with the DB unreachable a collection reports nothing (never a stale or
    zero value) and logs metrics.gauge_query_failed with the class only."""
    settings = api.rt.settings.model_copy(
        update={"db_port": free_port(), "payroll_gauge_query_timeout_ms": 300}
    )
    source = PayrollGaugeSource(settings)
    try:
        register_collection_gauges(source)
        assert otel_memory.metric_points("outbox_unsent") == []
    finally:
        source.close()
    [failed, *_] = caplog_json.events("metrics.gauge_query_failed")
    assert failed["gauge"] in NAMES
    assert set(failed) >= {"gauge", "error_type"}

"""scripts/ci/soak_resources.py: soak memory and consumer-lag checks (PAY-NFR-05; LLD §3.15).

PAY-NFR-05: the nightly soak shows no memory growth (RSS slope < 5% over the soak) and no
consumer-lag growth. The decision logic runs against a fake Prometheus; the PromQL itself runs
in promtool (the pinned Prometheus image) on synthetic series, so a query that Prometheus
evaluates differently from what the check assumes fails here rather than at night.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from pycommon.testing import find_repo_root

ROOT = find_repo_root()
sys.path.insert(0, str(ROOT))
from scripts.ci.soak_resources import (  # noqa: E402  (repo root added just above)
    PYTHON_SERVICES,
    Query,
    check,
    lag_last_query,
    lag_slope_query,
    main,
    memory_query,
)

PROMETHEUS_IMAGE = "prom/prometheus:v3.15.1"  # same pin as scripts/ci/lib.sh
START, END = 1_000_000.0, 1_001_800.0  # a 30 min soak
WINDOW = int(END - START)

Samples = list[tuple[dict[str, str], float]]


def fake(memory: dict[str, float], lag: dict[str, tuple[float, float]] | None = None) -> Query:
    """memory: service -> growth fraction; lag: stream -> (last value, slope per second)."""
    lag = {"stream:payout.requested": (0.0, 0.0)} if lag is None else lag

    def query(promql: str, at: float) -> Samples:
        assert at == END
        if promql == memory_query(WINDOW):
            return [({"service_name": s}, v) for s, v in memory.items()]
        if promql == lag_last_query(WINDOW):
            return [({"stream": s, "group": "payout-worker"}, v[0]) for s, v in lag.items()]
        if promql == lag_slope_query(WINDOW):
            return [({"stream": s, "group": "payout-worker"}, v[1]) for s, v in lag.items()]
        raise AssertionError(f"unexpected query {promql}")

    return query


FLAT = dict.fromkeys(PYTHON_SERVICES, 0.01)


def test_flat_memory_and_no_lag_pass() -> None:
    """PAY-NFR-05: every service under 5% and a zero lag pass, with one line per series."""
    report = check(fake(FLAT), START, END)
    assert report.ok, report.violations
    assert len(report.lines) == len(PYTHON_SERVICES) + 1


@pytest.mark.parametrize(("growth", "ok"), [(0.049, True), (0.05, False), (0.2, False)])
def test_rss_slope_limit_is_five_percent(growth: float, ok: bool) -> None:
    """PAY-NFR-05: RSS slope < 5% over the soak; 5% itself fails."""
    report = check(fake({**FLAT, "payroll": growth}), START, END)
    assert report.ok is ok
    if not ok:
        assert report.violations == [f"memory: payroll RSS grew {growth:+.1%} over the soak"]


def test_missing_service_series_fails() -> None:
    """PAY-NFR-05: a service without a memory series fails instead of passing silently."""
    report = check(fake({k: v for k, v in FLAT.items() if k != "payout-worker"}), START, END)
    assert report.violations == ["memory: no process_memory_usage_bytes series for payout-worker"]


@pytest.mark.parametrize(
    ("last", "slope", "ok"),
    [(0.0, 0.0, True), (12.0, -0.1, True), (0.0, 0.01, True), (12.0, 0.01, False)],
)
def test_lag_growth_is_positive_and_rising(last: float, slope: float, ok: bool) -> None:
    """PAY-NFR-05: no consumer-lag growth = never both > 0 and rising (ConsumerLagGrowing)."""
    report = check(fake(FLAT, {"stream:payout.requested": (last, slope)}), START, END)
    assert report.ok is ok


def test_no_lag_series_fails() -> None:
    """PAY-NFR-05: without queue_consumer_lag the lag check cannot pass."""
    report = check(fake(FLAT, {}), START, END)
    assert report.violations == ["consumer lag: no queue_consumer_lag series in the soak window"]


def test_main_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    """PAY-NFR-05: 0 when both hold, 1 on a violation, 2 when the window is too short."""
    args = ["--prometheus", "http://127.0.0.1:1", "--start", str(START), "--end", str(END)]
    assert main(args, fake(FLAT)) == 0
    assert main(args, fake({**FLAT, "identity": 0.3})) == 1
    assert "FAILED memory: identity" in capsys.readouterr().err
    short = ["--prometheus", "http://127.0.0.1:1", "--start", str(END - 60), "--end", str(END)]
    assert main(short, fake(FLAT)) == 2


def test_unreachable_prometheus_exits_2() -> None:
    """PAY-NFR-05: no Prometheus is an error (2), not a pass."""
    args = ["--prometheus", "http://127.0.0.1:9", "--start", str(START), "--end", str(END)]
    assert main(args) == 2


def test_queries_in_promtool(tmp_path: Path) -> None:
    """PAY-NFR-05: the PromQL flags a 7.7% RSS climb and a rising lag, and passes flat series."""
    if not shutil.which("docker"):
        pytest.skip("docker is not available to run promtool")
    w = 600  # 10 min of 15 s samples; the series below cover 0..600 s
    (tmp_path / "rules.yml").write_text(
        textwrap.dedent(
            f"""\
            groups:
              - name: soak
                rules:
                  - record: soak:rss_over_limit
                    expr: ({memory_query(w)}) > bool 0.05
                  - record: soak:lag_growing
                    expr: (({lag_last_query(w)}) > bool 0) * (({lag_slope_query(w)}) > bool 0)
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "test.yml").write_text(
        textwrap.dedent(
            """\
            rule_files: [rules.yml]
            evaluation_interval: 15s
            tests:
              - interval: 15s
                input_series:
                  - series: 'process_memory_usage_bytes{service_name="identity"}'
                    values: '100000000+0x40'
                  - series: 'process_memory_usage_bytes{service_name="payroll"}'
                    values: '100000000+200000x40'
                  - series: 'queue_consumer_lag{stream="a",group="g"}'
                    values: '0x40'
                  - series: 'queue_consumer_lag{stream="b",group="g"}'
                    values: '0+1x40'
                  - series: 'queue_consumer_lag{stream="c",group="g"}'
                    values: '40-1x40'
                promql_expr_test:
                  - expr: soak:rss_over_limit
                    eval_time: 10m
                    exp_samples:
                      - { labels: 'soak:rss_over_limit{service_name="identity"}', value: 0 }
                      - { labels: 'soak:rss_over_limit{service_name="payroll"}', value: 1 }
                  - expr: soak:lag_growing
                    eval_time: 10m
                    exp_samples:
                      - { labels: 'soak:lag_growing{stream="a",group="g"}', value: 0 }
                      - { labels: 'soak:lag_growing{stream="b",group="g"}', value: 1 }
                      - { labels: 'soak:lag_growing{stream="c",group="g"}', value: 0 }
            """
        ),
        encoding="utf-8",
    )
    cmd = [
        "docker", "run", "--rm", "-v", f"{tmp_path}:/t:ro", "-w", "/t",
        "--entrypoint", "promtool", PROMETHEUS_IMAGE, "test", "rules", "test.yml",
    ]  # fmt: skip
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr

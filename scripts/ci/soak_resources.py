"""Soak resources: no memory and no consumer-lag growth (PAY-NFR-05; platform LLD §3.15).

Run by ``scripts/ci/nightly-load.sh`` right after ``load/soak.js``::

    python3 scripts/ci/soak_resources.py --prometheus http://127.0.0.1:$PROMETHEUS_PORT \\
        --start <epoch s> --end <epoch s>

Both checks are instant Prometheus queries evaluated at ``--end`` over the window
``[--start, --end]`` (the soak scenario's duration, not k6 setup):

- **Memory:** for each Python service (``identity``, ``payroll``, ``payout-worker``,
  ``bank-stub``), the growth of ``process_memory_usage_bytes`` (the system-metrics
  instrumentation's ``process.memory.usage``, i.e. RSS) over the soak, as the least-squares slope
  (``deriv``) times the window, divided by the window's mean. It must be < ``--rss-limit``
  (default 0.05, "RSS slope < 5% over the soak"). A service with no series fails the check: a
  missing series would otherwise pass silently.
- **Consumer lag:** for each ``queue_consumer_lag`` (stream, group), the lag grows when the last
  value in the window is > 0 **and** the least-squares slope is > 0: the same condition as the
  ``ConsumerLagGrowing`` alert. No ``queue_consumer_lag`` series at all fails the check.

Exits 0 when both hold, 1 on a violation, 2 on bad arguments or an unreachable Prometheus.
Standard library only, so plain ``python3`` runs it.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

PYTHON_SERVICES = ("identity", "payroll", "payout-worker", "bank-stub")
RSS_LIMIT = 0.05
MIN_WINDOW_S = 120  # deriv needs several 15 s samples

Query = Callable[[str, float], list[tuple[dict[str, str], float]]]
"""(PromQL, evaluation time) -> [(labels, value)] for an instant vector."""


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def memory_query(window_s: int) -> str:
    w = f"{window_s}s"
    return (
        f"max by (service_name) (deriv(process_memory_usage_bytes[{w}]) * {window_s}"
        f" / avg_over_time(process_memory_usage_bytes[{w}]))"
    )


def lag_last_query(window_s: int) -> str:
    return f"max by (stream, group) (last_over_time(queue_consumer_lag[{window_s}s]))"


def lag_slope_query(window_s: int) -> str:
    return f"max by (stream, group) (deriv(queue_consumer_lag[{window_s}s]))"


def check(
    query: Query,
    start: float,
    end: float,
    *,
    rss_limit: float = RSS_LIMIT,
    services: Sequence[str] = PYTHON_SERVICES,
) -> Report:
    window_s = int(end - start)
    if window_s < MIN_WINDOW_S:
        raise ValueError(f"the soak window is {window_s} s; at least {MIN_WINDOW_S} s is needed")
    report = Report()

    memory = query(memory_query(window_s), end)
    growth = {labels.get("service_name", "?"): value for labels, value in memory}
    for service in services:
        if service not in growth:
            report.violations.append(f"memory: no process_memory_usage_bytes series for {service}")
            continue
        value = growth[service]
        verdict = "ok" if value < rss_limit else "GROWING"
        report.lines.append(
            f"memory {service}: RSS slope over the soak {value:+.1%}"
            f" (limit {rss_limit:.0%}): {verdict}"
        )
        if verdict != "ok":
            report.violations.append(f"memory: {service} RSS grew {value:+.1%} over the soak")

    def key(labels: dict[str, str]) -> tuple[str, str]:
        return labels.get("stream", "?"), labels.get("group", "?")

    last = {key(labels): value for labels, value in query(lag_last_query(window_s), end)}
    slope = {key(labels): value for labels, value in query(lag_slope_query(window_s), end)}
    if not last:
        report.violations.append("consumer lag: no queue_consumer_lag series in the soak window")
    for stream_group in sorted(last):
        stream, group = stream_group
        lag, per_s = last[stream_group], slope.get(stream_group, 0.0)
        growing = lag > 0 and per_s > 0
        report.lines.append(
            f"consumer lag {stream}/{group}: last {lag:g}, trend {per_s * window_s:+.1f}"
            f" entries over the soak: {'GROWING' if growing else 'ok'}"
        )
        if growing:
            report.violations.append(f"consumer lag: {stream}/{group} lag {lag:g} and rising")
    return report


def prometheus_query(base_url: str, timeout_s: float = 10.0) -> Query:
    def run(promql: str, at: float) -> list[tuple[dict[str, str], float]]:
        params = urllib.parse.urlencode({"query": promql, "time": f"{at:.3f}"})
        with urllib.request.urlopen(f"{base_url}/api/v1/query?{params}", timeout=timeout_s) as r:  # noqa: S310  (local Prometheus URL from the script)
            body = json.load(r)
        if body.get("status") != "success" or body["data"]["resultType"] != "vector":
            raise RuntimeError(f"unexpected Prometheus answer to {promql!r}: {body}")
        out: list[tuple[dict[str, str], float]] = []
        for item in body["data"]["result"]:
            value = float(item["value"][1])
            if not math.isnan(value):  # a series with one sample has no slope
                out.append((dict(item["metric"]), value))
        return out

    return run


def main(argv: Sequence[str] | None = None, query: Query | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--prometheus", required=True, help="Prometheus base URL")
    parser.add_argument("--start", type=float, required=True, help="soak start, epoch seconds")
    parser.add_argument("--end", type=float, required=True, help="soak end, epoch seconds")
    parser.add_argument("--rss-limit", type=float, default=RSS_LIMIT, help="max RSS growth")
    args = parser.parse_args(argv)
    try:
        report = check(
            query or prometheus_query(args.prometheus.rstrip("/")),
            args.start,
            args.end,
            rss_limit=args.rss_limit,
        )
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"soak resources: {exc}", file=sys.stderr)
        return 2
    for line in report.lines:
        print(f"soak resources: {line}")
    for violation in report.violations:
        print(f"soak resources: FAILED {violation}", file=sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""Alert rules, their promtool cases and runbooks (platform LLD §8.3; PAY-NFR-04, 10, 11).

- the rules are exactly the alerts of plan §11 and HLD §8, with the same severities;
- every alert has ``runbook_url`` = ``<repo>/docs/runbooks/<AlertName>.md`` and the file exists,
  with the skill's sections, and every query in it uses names that something emits;
- every alert has a promtool case that fires and one that stays quiet;
- the burn-rate alerts are the 14.4x / 6x multi-window rules on the 0.005 budget (ADR 0012);
- the SLI recording rules exclude the health and telemetry routes (C4);
- ``outcome`` matchers use only values the code emits (no ``failed`` sign-ins, C19);
- no metric name ends in ``_ratio`` (gauges never use unit ``"1"``, C63);
- every ``dashboard_url`` names a dashboard of ``grafana/dashboards``;
- ``scripts/ci/alert_drills.sh`` drills every paging alert.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from test_dashboards import (
    ROOT,
    all_dashboard_exprs,
    catalog,
    dashboards,
    known_names,
    metric_names,
)

OBS = ROOT / "deploy" / "observability" / "prometheus"
K8S_OBS = ROOT / "deploy" / "k8s" / "observability"
ALERT_FILES = (OBS / "rules" / "alerts.yml", K8S_OBS / "alerts-kubernetes.yml")
RECORDING = OBS / "rules" / "recording.yml"
TEST_FILES = (OBS / "tests" / "alerts_test.yml", K8S_OBS / "alerts-kubernetes_test.yml")
RUNBOOKS = ROOT / "docs" / "runbooks"
RUNBOOK_BASE = "https://github.com/Positron00/FinApp/blob/main/docs/runbooks/"
PLAN = ROOT / "docs" / "plan" / "architecture-plan.md"
HLD = ROOT / "docs" / "specs" / "payrun-0.1" / "design-high-level.md"
DRILLS = ROOT / "scripts" / "ci" / "alert_drills.sh"

SLI_EXCLUDED_ROUTES = ("/api/livez", "/api/readyz", "/api/telemetry")
BUDGET = 0.005
RUNBOOK_SECTIONS = (
    "## What it means",
    "## Impact",
    "## First checks",
    "## Likely causes",
    "## Mitigation",
    "## Confirm recovery",
)


# --------------------------------------------------------------------------- loading
@cache
def alert_rules() -> dict[str, dict[str, Any]]:
    rules: dict[str, dict[str, Any]] = {}
    for path in ALERT_FILES:
        for group in yaml.safe_load(path.read_text())["groups"]:
            for rule in group["rules"]:
                assert rule["alert"] not in rules, f"duplicate alert {rule['alert']}"
                rules[rule["alert"]] = rule
    return rules


@cache
def recording_rules() -> list[dict[str, Any]]:
    groups = yaml.safe_load(RECORDING.read_text())["groups"]
    return [r for g in groups for r in g["rules"]]


@cache
def promtool_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in TEST_FILES:
        cases.extend(yaml.safe_load(path.read_text())["tests"])
    return cases


def _section(path: Path, heading: str) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(heading)
    nxt = re.search(r"^## ", text[start + len(heading) :], re.M)
    return text[start : start + len(heading) + nxt.start()] if nxt else text[start:]


@cache
def plan_alerts() -> dict[str, str]:
    """Alert -> severity from the plan §11 table."""
    sec = _section(PLAN, "## 11. Observability")
    rows = re.findall(r"^\s*\| `([A-Z][A-Za-z]+)`[^|]*\| ([a-z]+)", sec, re.M)
    return dict(rows)


@cache
def hld_alerts() -> dict[str, str]:
    """Alert -> page | ticket/warning from the HLD §8 table."""
    sec = _section(HLD, "## 8. Observability")
    out: dict[str, str] = {}
    for kind, names in re.findall(r"^\| Alerts \(([a-z/]+)\) \| ([^|]+)\|", sec, re.M):
        for name in re.findall(r"`([A-Z][A-Za-z]+)`", names):
            out[name] = kind
    return out


def all_rule_exprs() -> Iterator[tuple[str, str]]:
    for name, rule in alert_rules().items():
        yield f"alert {name}", rule["expr"]
    for rule in recording_rules():
        yield f"record {rule['record']}", rule["expr"]


def runbook_path(alert: str) -> Path:
    return RUNBOOKS / f"{alert}.md"


# --------------------------------------------------------------------------- rule set
def test_rules_are_the_plan_and_hld_alerts() -> None:
    """Plan §11 and HLD §8 list the same alerts, and the rules implement exactly them."""
    plan, hld = plan_alerts(), hld_alerts()
    assert len(plan) >= 18, plan
    assert set(plan) == set(hld)
    assert set(alert_rules()) == set(plan)


@pytest.mark.parametrize("alert", sorted(alert_rules()))
def test_severity_matches_the_plan(alert: str) -> None:
    severity = alert_rules()[alert]["labels"]["severity"]
    assert severity == plan_alerts()[alert]
    assert (severity == "page") == (hld_alerts()[alert] == "page")


def test_paging_alerts() -> None:
    """HLD §8: the six paging alerts, including PayoutLateSettlement (C71)."""
    paging = {a for a, r in alert_rules().items() if r["labels"]["severity"] == "page"}
    assert paging == {
        "BffAvailabilityFastBurn",
        "PayoutSettlementSlow",
        "PayoutDLQ",
        "PayoutsStuck",
        "PayoutNeedsOperator",
        "PayoutLateSettlement",
    }


# --------------------------------------------------------------------------- runbooks
@pytest.mark.parametrize("alert", sorted(alert_rules()))
def test_runbook_url_points_at_an_existing_runbook(alert: str) -> None:
    annotations = alert_rules()[alert]["annotations"]
    assert annotations["runbook_url"] == f"{RUNBOOK_BASE}{alert}.md"
    assert runbook_path(alert).is_file(), f"missing docs/runbooks/{alert}.md"
    for key in ("summary", "description", "dashboard_url"):
        assert annotations.get(key), f"{alert}: no {key}"


def test_no_runbook_without_an_alert() -> None:
    orphans = {p.stem for p in RUNBOOKS.glob("*.md")} - set(alert_rules())
    assert not orphans


@pytest.mark.parametrize("alert", sorted(alert_rules()))
def test_runbook_structure(alert: str) -> None:
    """The skill's structure: meaning, impact, first checks (dashboard and a query), likely
    causes, mitigation, confirm recovery."""
    text = runbook_path(alert).read_text(encoding="utf-8")
    assert text.startswith(f"# Runbook: {alert}\n")
    positions = [text.find(s) for s in RUNBOOK_SECTIONS]
    assert all(p >= 0 for p in positions), f"{alert}: missing sections"
    assert positions == sorted(positions), f"{alert}: sections out of order"
    assert "/d/payrun-" in text, f"{alert}: no dashboard link"
    assert "```promql" in text, f"{alert}: no Prometheus query"
    checks = _section(runbook_path(alert), "## First checks")
    assert "```promql" in checks or "```logql" in checks


def _fenced(text: str, lang: str) -> list[str]:
    blocks = re.findall(rf"```{lang}\n(.*?)```", text, re.S)
    return [b.strip() for b in blocks]


@cache
def code_text() -> str:
    bases = (ROOT / "libs" / "pycommon" / "src", ROOT / "services", ROOT / "apps" / "web" / "src")
    return "\n".join(
        p.read_text(encoding="utf-8")
        for base in bases
        for p in base.rglob("*")
        if p.suffix in {".py", ".ts", ".tsx"} and "node_modules" not in p.parts
    )


@pytest.mark.parametrize("alert", sorted(alert_rules()))
def test_runbook_queries_use_real_names(alert: str) -> None:
    """Every PromQL block reads emitted or recorded metrics; every LogQL event exists in code."""
    text = runbook_path(alert).read_text(encoding="utf-8")
    for q in _fenced(text, "promql"):
        unknown = metric_names(q) - known_names()
        assert not unknown, f"{alert}: {q!r} uses {sorted(unknown)}"
    code = code_text()
    for q in _fenced(text, "logql"):
        assert re.match(r'\{service_name=~?"[^"]+"\}', q), f"{alert}: {q!r}"
        for value in re.findall(r'event=~?"([^"]+)"', q):
            for event in value.split("|"):
                assert f'"{event}"' in code, f"{alert}: log event {event!r} is not in the code"


# --------------------------------------------------------------------------- promtool cases
@pytest.mark.parametrize("alert", sorted(alert_rules()))
def test_every_alert_fires_and_stays_quiet_in_promtool(alert: str) -> None:
    fires = quiet = 0
    for case in promtool_cases():
        for t in case.get("alert_rule_test", []):
            if t["alertname"] != alert:
                continue
            if t["exp_alerts"]:
                fires += 1
                for exp in t["exp_alerts"]:
                    ann = exp.get("exp_annotations", {})
                    assert ann.get("runbook_url") == f"{RUNBOOK_BASE}{alert}.md", case["name"]
            else:
                quiet += 1
    assert fires, f"{alert}: no promtool case where it fires"
    assert quiet, f"{alert}: no promtool case where it stays quiet"


def test_promtool_files_load_the_rules() -> None:
    compose_tests = yaml.safe_load(TEST_FILES[0].read_text())
    assert compose_tests["rule_files"] == ["../rules/recording.yml", "../rules/alerts.yml"]


# --------------------------------------------------------------------------- SLOs
def test_burn_rate_alerts_follow_adr_0012() -> None:
    """PAY-NFR-04: page at 14.4x over 1 h and 5 m; ticket at 6x over 6 h and 30 m; budget 0.005."""
    fast, slow = alert_rules()["BffAvailabilityFastBurn"], alert_rules()["BffAvailabilitySlowBurn"]
    assert fast["expr"] == (
        f"sli:bff_errors:ratio_rate1h > (14.4 * {BUDGET}) and "
        f"sli:bff_errors:ratio_rate5m > (14.4 * {BUDGET})"
    )
    assert fast["for"] == "2m"
    assert fast["labels"]["severity"] == "page"
    assert slow["expr"] == (
        f"sli:bff_errors:ratio_rate6h > (6 * {BUDGET}) and "
        f"sli:bff_errors:ratio_rate30m > (6 * {BUDGET})"
    )
    assert slow["labels"]["severity"] == "ticket"
    windows = {r["record"] for r in recording_rules() if r["record"].startswith("sli:bff_errors")}
    assert windows == {f"sli:bff_errors:ratio_rate{w}" for w in ("5m", "30m", "1h", "6h")}


_BFF_SELECTOR = re.compile(r"bff_request_duration_seconds_(?:count|bucket|sum)\{([^}]*)\}")
_ROUTE_MATCHER = re.compile(r'route\s*(=~|!~|=|!=)\s*"([^"]*)"')


def _check_sli_selector(where: str, labels: str) -> None:
    m = _ROUTE_MATCHER.search(labels)
    assert m, f"{where}: a BFF SLI selector without a route matcher: {{{labels}}}"
    op, value = m.groups()
    regex = value if "~" in op else re.escape(value)
    for route in SLI_EXCLUDED_ROUTES:
        matched = re.fullmatch(regex, route) is not None
        excluded = matched if op.startswith("!") else not matched
        assert excluded, f"{where}: {route} is not excluded by route{op}{value!r} (C4)"


def test_sli_recording_rules_exclude_health_and_telemetry_c4() -> None:
    selectors = 0
    for rule in recording_rules():
        for labels in _BFF_SELECTOR.findall(rule["expr"]):
            _check_sli_selector(rule["record"], labels)
            selectors += 1
    assert selectors >= 10


def test_journeys_dashboard_routes_exclude_health_and_telemetry_c4() -> None:
    for where, expr in all_dashboard_exprs():
        if where.startswith("journeys.json"):
            for labels in _BFF_SELECTOR.findall(expr):
                _check_sli_selector(where, labels)


# --------------------------------------------------------------------------- names and labels
_SELECTOR = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\{([^}]*)\}")
_OUTCOME = re.compile(r'\boutcome\s*(=~|!~|=|!=)\s*"([^"]*)"')


def _selectors_with_outcome() -> Iterator[tuple[str, str, str, str]]:
    """(where, metric, op, value) for every outcome matcher in rules, promtool inputs and
    dashboards."""
    sources: list[tuple[str, str]] = list(all_rule_exprs()) + list(all_dashboard_exprs())
    for case in promtool_cases():
        sources += [(case["name"], s["series"]) for s in case.get("input_series", [])]
    for where, expr in sources:
        for metric, labels in _SELECTOR.findall(expr):
            for op, value in _OUTCOME.findall(labels):
                yield where, metric, op, value


def test_signin_outcome_is_never_failed_c19() -> None:
    bad = [
        f"{where}: {metric}{{outcome{op}{value!r}}}"
        for where, metric, op, value in _selectors_with_outcome()
        if metric == "auth_signins_total" and "failed" in value.split("|")
    ]
    header = "auth_signins_total uses outcome 'failure', never 'failed' (C19):\n"
    assert not bad, header + "\n".join(bad)


def test_outcome_values_are_emitted() -> None:
    """Every literal outcome a rule, test or dashboard matches is one the code emits."""
    allowed: dict[str, list[str]] = catalog()["outcomes"]
    bad = [
        f"{where}: {metric} outcome {v!r}"
        for where, metric, _op, value in _selectors_with_outcome()
        if metric in allowed
        for v in value.split("|")
        if re.fullmatch(r"[a-z_]+", v) and v not in allowed[metric]
    ]
    assert not bad, "\n".join(bad)


def test_rule_metrics_are_emitted() -> None:
    missing = [
        f"{where}: {sorted(metric_names(expr) - known_names())}"
        for where, expr in all_rule_exprs()
        if metric_names(expr) - known_names()
    ]
    assert not missing, "\n".join(missing)


def test_no_metric_name_has_a_ratio_suffix_c63() -> None:
    """Gauges never use unit "1", which the Collector's exporter turns into a ``_ratio`` suffix.
    (Recording rules such as ``sli:bff_errors:ratio_rate5m`` are colon names, not metrics.)"""
    used: set[str] = set(catalog()["app"]) | set(catalog()["auto"])
    for _where, expr in list(all_rule_exprs()) + list(all_dashboard_exprs()):
        used |= {n for n in metric_names(expr) if ":" not in n}
    assert not sorted(n for n in used if n.endswith("_ratio"))


_GAUGE_UNIT_ONE = re.compile(
    r"(?:observable_gauge|create_observable_gauge|create_gauge|createGauge|createObservableGauge)"
    r"\((?:[^()]|\([^()]*\))*?unit\s*[=:]\s*[\"']1[\"']",
    re.S,
)


def test_no_gauge_is_created_with_unit_one_c63() -> None:
    text = code_text()
    assert not _GAUGE_UNIT_ONE.findall(text)


def test_dashboard_urls_name_existing_dashboards() -> None:
    uids = {d["uid"] for d in dashboards().values()}
    for alert, rule in alert_rules().items():
        url = rule["annotations"]["dashboard_url"]
        uid = url.rsplit("/d/", 1)[1]
        assert uid in uids, f"{alert}: {url}"


# --------------------------------------------------------------------------- drills
def test_alert_drills_cover_every_paging_alert() -> None:
    text = DRILLS.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    for alert, rule in alert_rules().items():
        if rule["labels"]["severity"] == "page":
            assert f"drill_{alert}()" in text, f"no drill for {alert}"

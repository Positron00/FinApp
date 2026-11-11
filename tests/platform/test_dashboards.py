"""Grafana dashboards as code (platform LLD §8.7-8.8; PAY-NFR-10; C55, C63, C65).

- every dashboard is valid JSON, with the five uids of the LLD, each used once;
- panel ids are unique per dashboard and every panel sits on the 24-column grid;
- every datasource reference names a uid (and type) that provisioning creates;
- every Prometheus expression uses only metric names that something emits:
  ``deploy/observability/metrics-catalog.json`` (checked against the code here, both ways) or a
  recording rule of ``prometheus/rules/recording.yml``;
- histogram panels show exemplars (they link to Tempo through the Prometheus datasource).

``metric_names`` is also used by ``test_rules_meta.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from pycommon.testing import find_repo_root

ROOT = find_repo_root()
OBS = ROOT / "deploy" / "observability"
DASHBOARDS = OBS / "grafana" / "dashboards"
DATASOURCES = OBS / "grafana" / "provisioning" / "datasources" / "datasources.yaml"
CATALOG = OBS / "metrics-catalog.json"
RECORDING = OBS / "prometheus" / "rules" / "recording.yml"

EXPECTED_UIDS = {
    "service-overview.json": "payrun-service-overview",
    "journeys.json": "payrun-journeys",
    "queues.json": "payrun-queues",
    "bank.json": "payrun-bank",
    "platform.json": "payrun-platform",
}
HISTOGRAM_SUFFIXES = ("_bucket", "_count", "_sum")

# --------------------------------------------------------------------------- PromQL names
_LABEL_VALUES = re.compile(r"label_values\(\s*([^,()]*?)\s*,\s*\w+\s*\)")
_NAME_MATCHER = re.compile(r'__name__\s*(=~|=)\s*"((?:[^"\\]|\\.)*)"')
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`[^`]*`')
_BRACES = re.compile(r"\{[^{}]*\}")
_RANGE = re.compile(r"\[[^\[\]]*\]")
_CLAUSE = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^()]*\)")
_IDENT = re.compile(r"(?<![\w:.$])([A-Za-z_:][A-Za-z0-9_:]*)(?![\w:])(\s*\()?")
_KEYWORDS = frozenset(
    {"and", "or", "unless", "bool", "offset", "by", "without", "on", "ignoring"}
    | {"group_left", "group_right", "inf", "nan", "Inf", "NaN", "atan2"}
)


def metric_names(expr: str) -> set[str]:
    """The metric names a PromQL expression reads (functions, labels and keywords excluded).

    ``{__name__=~"a|b(_total)?"}`` yields ``a`` and ``b``: the optional ``_total`` is how the rules
    match Collector 0.164's unsuffixed self-metrics as well as later suffixed ones (C65).
    """
    expr = _LABEL_VALUES.sub(r"\1", expr)
    names: set[str] = set()
    for op, value in _NAME_MATCHER.findall(expr):
        alternatives = [value] if op == "=" else value.replace("(_total)?", "").split("|")
        names.update(a for a in alternatives if a)
    expr = _STRING.sub('""', expr)
    expr = _BRACES.sub("", expr)
    while _RANGE.search(expr):
        expr = _RANGE.sub("", expr)
    expr = _CLAUSE.sub("", expr)
    for name, call in _IDENT.findall(expr):
        if not call and name not in _KEYWORDS:
            names.add(name)
    return names


# --------------------------------------------------------------------------- catalog
@cache
def catalog() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(CATALOG.read_text())
    return data


def _expand(name: str, kind: str) -> set[str]:
    if kind == "histogram":
        return {name, *(name + s for s in HISTOGRAM_SUFFIXES)}
    return {name}


@cache
def emitted_names() -> frozenset[str]:
    """Every metric name the stack emits, per the catalog (histograms expanded)."""
    c = catalog()
    out: set[str] = set()
    for section in ("app", "auto"):
        for name, meta in c[section].items():
            out |= _expand(name, meta["type"])
    for names in c["external"].values():
        out.update(names)
    return frozenset(out)


@cache
def recorded_names() -> frozenset[str]:
    groups = yaml.safe_load(RECORDING.read_text())["groups"]
    return frozenset(r["record"] for g in groups for r in g["rules"])


def known_names() -> frozenset[str]:
    return emitted_names() | recorded_names()


# --------------------------------------------------------------------------- dashboards
@cache
def dashboards() -> dict[str, dict[str, Any]]:
    return {p.name: json.loads(p.read_text()) for p in sorted(DASHBOARDS.glob("*.json"))}


def panels(items: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for p in items:
        yield p
        yield from panels(p.get("panels", []))


def datasource_refs(dash: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """(where, datasource) for every datasource reference in a dashboard."""
    for p in panels(dash["panels"]):
        if "datasource" in p:
            yield f"panel {p['id']}", p["datasource"]
        for t in p.get("targets", []):
            yield f"panel {p['id']} target {t.get('refId')}", t["datasource"]
    for v in dash.get("templating", {}).get("list", []):
        if "datasource" in v:
            yield f"variable {v['name']}", v["datasource"]
    for a in dash.get("annotations", {}).get("list", []):
        if "datasource" in a:
            yield f"annotation {a['name']}", a["datasource"]


def prom_exprs(dash: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """(where, expr) for every Prometheus query: targets, variables and annotations."""
    for p in panels(dash["panels"]):
        for t in p.get("targets", []):
            if t["datasource"]["type"] == "prometheus":
                yield f"panel {p['id']} ({p.get('title')}) {t['refId']}", t["expr"]
    for v in dash.get("templating", {}).get("list", []):
        if v.get("datasource", {}).get("type") == "prometheus":
            query = v["query"]["query"] if isinstance(v["query"], dict) else v["query"]
            yield f"variable {v['name']}", query
    for a in dash.get("annotations", {}).get("list", []):
        if a.get("datasource", {}).get("type") == "prometheus" and a.get("expr"):
            yield f"annotation {a['name']}", a["expr"]


def all_dashboard_exprs() -> Iterator[tuple[str, str]]:
    for file, dash in dashboards().items():
        for where, expr in prom_exprs(dash):
            yield f"{file} {where}", expr


# --------------------------------------------------------------------------- the extractor
@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        (
            'histogram_quantile(0.95, sum by (le, route) (rate(x_bucket{route=~"/a/[^/]+"}[5m])))',
            {"x_bucket"},
        ),
        ('(sum(rate({__name__=~"otelcol_a(_total)?"}[5m])) or vector(0)) > 0', {"otelcol_a"}),
        ("sli:bff_errors:ratio_rate1h / 0.005", {"sli:bff_errors:ratio_rate1h"}),
        ("max(a / (b > 0)) > 0.8", {"a", "b"}),
        ("x and on(stream) deriv(y[10m]) > 0", {"x", "y"}),
        ("1 - avg_over_time(z[30d:1h]) / 0.005", {"z"}),
        ('label_values({__name__=~"m1|m2"}, service_name)', {"m1", "m2"}),
        ("label_values(up, job)", {"up"}),
        ('sum(increase(c_total{outcome="failure"}[$__rate_interval])) offset 1h', {"c_total"}),
    ],
)
def test_metric_names_extractor(expr: str, expected: set[str]) -> None:
    assert metric_names(expr) == expected


# --------------------------------------------------------------------------- dashboards
def test_the_five_dashboards_exist_with_unique_uids() -> None:
    """PAY-NFR-10: service overview, journeys, queues and workers, bank, platform."""
    found = {file: d["uid"] for file, d in dashboards().items()}
    assert found == EXPECTED_UIDS
    titles = [d["title"] for d in dashboards().values()]
    assert len(set(titles)) == len(titles)


@pytest.mark.parametrize("file", sorted(EXPECTED_UIDS))
def test_dashboard_settings(file: str) -> None:
    """LLD §8.8: default range 6 h, refresh 30 s, read-only, tagged for the dashboard links."""
    d = dashboards()[file]
    assert d["time"] == {"from": "now-6h", "to": "now"}
    assert d["refresh"] == "30s"
    assert d["editable"] is False
    assert "payrun" in d["tags"]
    assert "id" not in d or d["id"] is None  # provisioning assigns ids


@pytest.mark.parametrize("file", sorted(EXPECTED_UIDS))
def test_panel_ids_are_unique_and_on_the_grid(file: str) -> None:
    ids = [p["id"] for p in panels(dashboards()[file]["panels"])]
    assert len(ids) == len(set(ids)), f"duplicate panel ids in {file}"
    for p in panels(dashboards()[file]["panels"]):
        g = p["gridPos"]
        where = (file, p["id"], g)
        assert g["x"] >= 0, where
        assert g["w"] > 0, where
        assert g["x"] + g["w"] <= 24, where
        assert g["h"] > 0, where


def test_datasource_uids_match_provisioning() -> None:
    provisioned = {
        ds["uid"]: ds["type"] for ds in yaml.safe_load(DATASOURCES.read_text())["datasources"]
    }
    assert set(provisioned) == {"prometheus", "tempo", "loki"}
    bad = [
        f"{file} {where}: {ds}"
        for file, dash in dashboards().items()
        for where, ds in datasource_refs(dash)
        if provisioned.get(ds.get("uid")) != ds.get("type")
    ]
    assert not bad, "datasource refs that provisioning does not create:\n" + "\n".join(bad)


def test_exemplar_destination_is_tempo() -> None:
    """Exemplars on histogram panels link to traces through the provisioned datasource."""
    prom = next(
        ds
        for ds in yaml.safe_load(DATASOURCES.read_text())["datasources"]
        if ds["uid"] == "prometheus"
    )
    dests = prom["jsonData"]["exemplarTraceIdDestinations"]
    assert dests == [{"name": "trace_id", "datasourceUid": "tempo"}]


def test_every_panel_metric_is_emitted() -> None:
    """Each name a dashboard query reads is in the catalog or is a recording rule (PAY-NFR-10)."""
    known = known_names()
    missing = [
        f"{where}: {sorted(metric_names(expr) - known)}"
        for where, expr in all_dashboard_exprs()
        if metric_names(expr) - known
    ]
    assert not missing, "metric names nobody emits:\n" + "\n".join(missing)


def test_every_query_reads_a_metric() -> None:
    empty = [where for where, expr in all_dashboard_exprs() if not metric_names(expr)]
    assert not empty


def test_histogram_panels_show_exemplars() -> None:
    """LLD §8.8: histogram panels show exemplars (time series and heatmaps over ``_bucket``)."""
    off = [
        f"{file} panel {p['id']} ({p['title']}) {t['refId']}"
        for file, dash in dashboards().items()
        for p in panels(dash["panels"])
        for t in p.get("targets", [])
        if t["datasource"]["type"] == "prometheus"
        and "_bucket" in t["expr"]
        and not t.get("exemplar")
    ]
    assert not off, "histogram queries without exemplars:\n" + "\n".join(off)


def test_journeys_has_one_row_per_journey() -> None:
    rows = [p["title"] for p in dashboards()["journeys.json"]["panels"] if p["type"] == "row"]
    assert [r.split()[0] for r in rows] == ["SLO", "J1", "J2", "J3", "J4", "J5"]


# --------------------------------------------------------------------------- catalog vs code
_PY_INSTRUMENT = re.compile(
    r"\b(?:create_counter|create_up_down_counter|create_histogram|create_gauge|"
    r"create_observable_gauge|create_observable_counter|histogram|observable_gauge)"
    r'\(\s*"([a-z][a-z0-9_]*)"'
)
_PY_NAME_CONSTANT = re.compile(r'^([A-Z][A-Z0-9_]*): Final = "([a-z][a-z0-9_]*_[a-z0-9_]+)"$', re.M)
_TS_INSTRUMENT = re.compile(
    r"\bcreate(?:Counter|UpDownCounter|Histogram|Gauge|ObservableGauge|ObservableCounter)"
    r'\(\s*"([a-z][a-z0-9_]*)"'
)


def _sources() -> Iterator[Path]:
    for base in (ROOT / "libs" / "pycommon" / "src", *sorted((ROOT / "services").glob("*/src"))):
        yield from (p for p in base.rglob("*.py") if "tests" not in p.parts)
    web = ROOT / "apps" / "web" / "src"
    yield from (
        p
        for p in web.rglob("*.ts")
        if "node_modules" not in p.parts and not p.name.endswith((".test.ts", ".spec.ts"))
    )


def code_instruments() -> dict[str, Path]:
    """Instrument names the code creates: literals passed to a create call, plus the gauge-name
    constants of a module that registers collection-time gauges (identity, payroll)."""
    found: dict[str, Path] = {}
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        pattern = _TS_INSTRUMENT if path.suffix == ".ts" else _PY_INSTRUMENT
        for name in pattern.findall(text):
            found.setdefault(name, path)
        if path.suffix == ".py" and "observable_gauge(" in text:
            for const, name in _PY_NAME_CONSTANT.findall(text):
                # passed as a gauge name: observable_gauge(NAME, unit=...) or a (NAME, "unit", ...)
                # row of a gauge table such as payroll's GAUGES
                used = rf'(?:observable_gauge\(|\()\s*{const}\s*,\s*(?:unit\s*=\s*)?"'
                if re.search(used, text):
                    found.setdefault(name, path)
    return found


def test_catalog_app_metrics_are_created_in_their_source() -> None:
    bad = []
    for name, meta in catalog()["app"].items():
        src = ROOT / meta["source"]
        if not src.is_file() or f'"{meta["instrument"]}"' not in src.read_text(encoding="utf-8"):
            bad.append(f"{name}: {meta['instrument']!r} not in {meta['source']}")
    assert not bad, "\n".join(bad)


def test_every_instrument_in_the_code_is_in_the_catalog() -> None:
    """A new instrument must be listed (with its Prometheus name) before a dashboard can use it."""
    listed = {meta["instrument"] for meta in catalog()["app"].values()}
    found = code_instruments()
    assert found, "the instrument scan found nothing: the regexes are out of date"
    unlisted = {n: str(p.relative_to(ROOT)) for n, p in found.items() if n not in listed}
    assert not unlisted, f"instruments missing from metrics-catalog.json: {unlisted}"


def test_catalog_prometheus_names_follow_the_exporter_rules() -> None:
    """Counters end in ``_total``; ``s`` histograms and gauges end in ``_seconds`` (C63, C65)."""
    for name, meta in catalog()["app"].items():
        if meta["type"] == "counter":
            assert name == meta["instrument"] + "_total", name
        else:
            assert name.removesuffix("_seconds") == meta["instrument"].removesuffix("_seconds")

"""Helpers for the stack-level fault tests: the stack's settings, the Toxiproxy API, Compose
container control, superuser reads of Postgres and Redis, and pay runs driven through payroll.

Every wait polls observable state (HTTP, SQL, Redis, container logs) against a deadline; nothing
sleeps for a fixed time to let a fault "settle".
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.mint_token import mint  # noqa: E402  (repo root added just above)

FINAL_RUN: Final = frozenset({"paid", "partially_failed"})
POLL_S: Final = 1.0
COMPOSE_ARGS: Final = (
    "docker", "compose", "--env-file", str(REPO_ROOT / ".env"),
    "-f", str(REPO_ROOT / "deploy/compose/compose.yaml"),
    "-f", str(REPO_ROOT / "deploy/compose/compose.test.yaml"),
    "--profile", "core", "--profile", "obs", "--profile", "test",
)  # fmt: skip
WEB_CONFIG_TS: Final = REPO_ROOT / "apps" / "web" / "src" / "lib" / "config.ts"
_log = logging.getLogger("resilience")
# Payout timing stays at production values in the test profile (C53). The waits use the values
# `docker compose config` resolves for payroll and payout-worker (it needs .env, as the stack does);
# a key missing from a config that was read is an error. The defaults below (Compose's
# `x-payout-timing` values) are used only when Compose itself is unavailable.
TIMING_DEFAULTS: Final = {
    "payout-worker": {
        "PAYOUT_CLAIM_MIN_IDLE_MS": 60000, "PAYOUT_MAX_DELIVERIES": 5, "BANK_TOTAL_DEADLINE_S": 15,
    },
    "payroll": {"PAYOUT_REPUBLISH_AFTER_S": 360, "MAINTENANCE_INTERVAL_S": 60},
}  # fmt: skip


def read_compose_config() -> tuple[dict[str, Any] | None, str]:
    """``docker compose config --format json`` and a description of the source; ``None`` when
    Compose can't produce it (no docker, no .env, a timeout). Output that isn't JSON raises."""
    try:
        out = subprocess.run(
            [*COMPOSE_ARGS, "config", "--format", "json"],
            capture_output=True, text=True, check=True, timeout=60, cwd=REPO_ROOT,
        ).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        source = f"defaults in tests/resilience/_stack.py (docker compose config failed: {exc})"
        _log.warning("resilience timings: %s", source)
        return None, source
    config: dict[str, Any] = json.loads(out)
    _log.info("resilience timings: docker compose config")
    return config, "docker compose config"


def _service_env(config: Mapping[str, Any], service: str) -> Mapping[str, Any]:
    spec: Mapping[str, Any] = config.get("services", {}).get(service) or {}
    env: Mapping[str, Any] = spec.get("environment") or {}
    return env


def compose_timing(config: Mapping[str, Any] | None) -> dict[str, int]:
    """The payout timing keys of TIMING_DEFAULTS from a ``docker compose config`` (only those keys
    are read: the services' environments also hold secrets). ``None`` (Compose unavailable) gives
    the defaults; a config that lacks any key raises ``LookupError``."""
    if config is None:
        return {k: v for defaults in TIMING_DEFAULTS.values() for k, v in defaults.items()}
    timing: dict[str, int] = {}
    missing: list[str] = []
    for service, defaults in TIMING_DEFAULTS.items():
        env = _service_env(config, service)
        for key in defaults:
            if key in env:
                timing[key] = int(env[key])
            else:
                missing.append(f"{service}.{key}")
    if missing:
        raise LookupError(f"docker compose config does not set {', '.join(missing)}")
    return timing


def web_breaker_failures(config: Mapping[str, Any] | None) -> int:
    """The web's BREAKER_FAILURES: Compose's value for ``web`` when it sets one, else the default
    in ``apps/web/src/lib/config.ts`` (the BFF -> identity breaker is the web's, not pycommon's)."""
    if config is not None:
        env = _service_env(config, "web")
        if "BREAKER_FAILURES" in env:
            return int(env["BREAKER_FAILURES"])
    found = re.search(r"\bBREAKER_FAILURES: int\((\d+)", WEB_CONFIG_TS.read_text(encoding="utf-8"))
    if not found:
        raise LookupError(f"no BREAKER_FAILURES default in {WEB_CONFIG_TS}")
    return int(found.group(1))


_CONFIG, TIMING_SOURCE = read_compose_config()
_TIMING: Final = compose_timing(_CONFIG)
BREAKER_FAILURES: Final = web_breaker_failures(_CONFIG)
# The worker reclaims a message after CLAIM_IDLE_S idle and dead-letters it at MAX_DELIVERIES;
# each delivery's bank call has BANK_DEADLINE_S; payroll re-publishes REPUBLISH_AFTER_S after the
# last publish, on its MAINTENANCE_S tick. These bound the waits below.
CLAIM_IDLE_S: Final = _TIMING["PAYOUT_CLAIM_MIN_IDLE_MS"] // 1000
MAX_DELIVERIES: Final = _TIMING["PAYOUT_MAX_DELIVERIES"]
BANK_DEADLINE_S: Final = _TIMING["BANK_TOTAL_DEADLINE_S"]
REPUBLISH_AFTER_S: Final = _TIMING["PAYOUT_REPUBLISH_AFTER_S"]
MAINTENANCE_S: Final = _TIMING["MAINTENANCE_INTERVAL_S"]
REPUBLISH_WAIT_S: Final = REPUBLISH_AFTER_S + 2 * MAINTENANCE_S + 60
DLQ_WAIT_S: Final = MAX_DELIVERIES * CLAIM_IDLE_S + 90
# Telemetry reaches Prometheus within one SDK export (15 s) plus one scrape (15 s); the waits on a
# metric allow three of each.
METRIC_WAIT_S: Final = 90
STREAM: Final = "stream:payout.requested"
DLQ: Final = "stream:payout.requested.dlq"


def wait_until[T](
    what: str, probe: Callable[[], T | None], timeout: float, interval: float = POLL_S
) -> T:
    """Polls ``probe`` until it returns a truthy value; fails after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    last: T | None = None
    while True:
        try:
            last = probe()
        except httpx.HTTPError, subprocess.CalledProcessError:
            last = None
        if last:
            return last
        assert time.monotonic() < deadline, f"timed out after {timeout:.0f}s waiting for {what}"
        time.sleep(interval)


def error_code(r: httpx.Response) -> str | None:
    try:
        return str(r.json()["error"]["code"])
    except ValueError, KeyError, TypeError:
        return None


Series = dict[frozenset[tuple[str, str]], float]


def counter_increase(before: Series, after: Series) -> float:
    """The rise of a counter from one ``Stack.prom`` snapshot to the next, summed per series."""
    return sum(max(0.0, v - before.get(k, 0.0)) for k, v in after.items())


@dataclass(frozen=True)
class Stack:
    env: dict[str, str]
    owner_id: str
    approver_id: str = ""

    def url(self, port_key: str) -> str:
        return f"http://127.0.0.1:{self.env[port_key]}"

    def owner_token(self, aud: str) -> str:
        roles = ["admin", "approver", "payroll_admin"]
        return mint(aud, self.owner_id, roles, name="Owner", env=self.env)

    def approver_token(self, aud: str) -> str:
        """The seeded approver (a second person, so dual approval holds in the full seed)."""
        return mint(aud, self.approver_id, ["approver"], name="Seed Approver", env=self.env)

    # ------------------------------------------------------------------------------ Compose
    def compose(self, *args: str, timeout: float = 180) -> str:
        out = subprocess.run(
            [*COMPOSE_ARGS, *args],
            capture_output=True, text=True, check=True, timeout=timeout, cwd=REPO_ROOT,
        )  # fmt: skip
        return out.stdout

    def psql(self, sql: str) -> list[list[str]]:
        """Rows of a superuser query (``|``-separated, unaligned): test-side reads only."""
        out = self.compose(
            "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "payrun",
            "-At", "-v", "ON_ERROR_STOP=1", "-c", sql,
        )  # fmt: skip
        return [line.split("|") for line in out.splitlines() if line]

    def redis(self, *cmd: str) -> str:
        """A redis-cli command as the admin (``default``) user. The password stays inside the
        container (REDISCLI_AUTH from its own environment), never on a command line."""
        script = 'REDISCLI_AUTH="$REDIS_ADMIN_PASSWORD" exec redis-cli --raw "$@"'
        return self.compose("exec", "-T", "redis", "sh", "-c", script, "redis-cli", *cmd).strip()

    def redis_int(self, *cmd: str) -> int:
        """The first line of a raw reply as an integer (XLEN, the XPENDING summary count)."""
        first = (self.redis(*cmd).splitlines() or ["0"])[0].strip()
        return int(first) if first.lstrip("-").isdigit() else 0

    def pending(self) -> int:
        """Entries delivered to the worker group and not yet acknowledged (its PEL)."""
        return self.redis_int("XPENDING", STREAM, "payout-worker")

    def logs_since(self, service: str, since: datetime) -> str:
        return self.compose("logs", "--no-color", "--since", since.isoformat(), service)

    # --------------------------------------------------------------------------- Prometheus
    def prom(self, expr: str) -> Series:
        """An instant query on the stack's Prometheus (PROMETHEUS_PORT, published by the test
        profile): each sample's labels (``__name__`` dropped) and value."""
        r = httpx.get(
            f"{self.url('PROMETHEUS_PORT')}/api/v1/query", params={"query": expr}, timeout=10
        )
        r.raise_for_status()
        data = r.json()["data"]
        if data["resultType"] == "scalar":
            return {frozenset(): float(data["result"][1])}
        return {
            frozenset((k, v) for k, v in s["metric"].items() if k != "__name__"): float(
                s["value"][1]
            )
            for s in data["result"]
        }

    def wait_prom(
        self, what: str, expr: str, ok: Callable[[float], bool], timeout: float = METRIC_WAIT_S
    ) -> float:
        """Polls ``expr`` until its largest sample satisfies ``ok``; returns that value."""

        def probe() -> tuple[float] | None:
            values = self.prom(expr).values()
            top = max(values) if values else None
            return (top,) if top is not None and ok(top) else None

        return wait_until(f"{what} ({expr})", probe, timeout, interval=3.0)[0]

    def wait_increase(
        self, what: str, expr: str, before: Series, at_least: float, timeout: float = METRIC_WAIT_S
    ) -> float:
        """Polls a counter ``expr`` until it rose by ``at_least`` since the ``before`` snapshot
        (``prom(expr)``), summed per series: a series that is new since then (a restarted
        process has a new ``instance``) counts in full, one that expired (a dead process's
        series leave after the Collector's 2 m metric_expiration) counts as no change."""

        def probe() -> tuple[float] | None:
            grown = counter_increase(before, self.prom(expr))
            return (grown,) if grown >= at_least else None

        return wait_until(f"{what} (+{at_least:g} in {expr})", probe, timeout, interval=3.0)[0]


class Toxiproxy:
    def __init__(self, base: str) -> None:
        self.http = httpx.Client(base_url=base, timeout=5)

    def add(
        self, proxy: str, name: str, type_: str, stream: str = "downstream", **attrs: int
    ) -> None:
        body = {"name": name, "type": type_, "stream": stream, "attributes": attrs}
        self.http.post(f"/proxies/{proxy}/toxics", json=body).raise_for_status()

    def remove(self, proxy: str, name: str) -> None:
        self.http.delete(f"/proxies/{proxy}/toxics/{name}").raise_for_status()

    def set_enabled(self, proxy: str, enabled: bool) -> None:
        """A disabled proxy closes its connections and refuses new ones (the upstream is down)."""
        self.http.post(f"/proxies/{proxy}", json={"enabled": enabled}).raise_for_status()

    def reset(self) -> None:
        """Re-enable every proxy and remove every toxic (POST /reset)."""
        self.http.post("/reset").raise_for_status()


@dataclass
class Containers:
    """Stops, kills and pauses Compose services; teardown brings every touched one back."""

    stack: Stack
    touched: set[str] = field(default_factory=set)
    paused: set[str] = field(default_factory=set)

    def kill(self, service: str) -> None:
        self.touched.add(service)
        self.stack.compose("kill", "-s", "SIGKILL", service)

    def stop(self, service: str) -> None:
        self.touched.add(service)
        self.stack.compose("stop", "-t", "10", service)

    def restart(self, service: str) -> None:
        self.touched.add(service)
        self.stack.compose("restart", "-t", "10", service)
        self.wait_healthy(service)

    def pause(self, service: str) -> None:
        self.touched.add(service)
        self.paused.add(service)
        self.stack.compose("pause", service)

    def unpause(self, service: str) -> None:
        self.stack.compose("unpause", service)
        self.paused.discard(service)

    def start(self, service: str) -> None:
        """Starts the service (no dependency recreation) and waits for its healthcheck."""
        self.stack.compose("start", service)
        self.wait_healthy(service)

    def wait_healthy(self, service: str, timeout: float = 90) -> None:
        def healthy() -> bool:
            cid = self.stack.compose("ps", "-q", service).strip()
            if not cid:
                return False
            out = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}} {{.State.Health.Status}}", cid],
                capture_output=True, text=True, check=True,
            )  # fmt: skip
            return out.stdout.split() == ["running", "healthy"]

        wait_until(f"{service} healthy", healthy, timeout)

    def restore(self) -> None:
        for service in sorted(self.paused):
            self.unpause(service)
        for service in sorted(self.touched):
            self.stack.compose("start", service)
        for service in sorted(self.touched):
            self.wait_healthy(service)


# ---------------------------------------------------------------------------------- pay runs
class Payroll:
    """Pay runs through payroll's own API (published by compose.test.yaml), as the owner and
    the seeded approver. Independent of the web and of Redis (the service limiter fails open)."""

    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.http = httpx.Client(base_url=stack.url("PAYROLL_PORT"), timeout=30)

    def call(
        self, method: str, path: str, *, as_approver: bool = False, **kw: Any
    ) -> httpx.Response:
        token = (
            self.stack.approver_token("payroll")
            if as_approver
            else self.stack.owner_token("payroll")
        )
        headers = {"Authorization": f"Bearer {token}", **kw.pop("headers", {})}
        return self.http.request(method, path, headers=headers, **kw)

    def _pages(self, path: str, **params: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            q: dict[str, str | int] = {"limit": 100, **params}
            if cursor:
                q["cursor"] = cursor
            r = self.call("GET", path, params=q)
            assert r.status_code == 200, (path, r.status_code, error_code(r))
            body = r.json()
            items += body["items"]
            cursor = body.get("next_cursor")
            if not cursor:
                return items

    def _free_month(self) -> date:
        """The first monthly period after next month without a run (PAY-300 overlap check)."""
        used = {
            r["period_start"] for r in self._pages("/v1/pay-runs") if r["schedule"] == "monthly"
        }
        today = datetime.now(UTC).date()
        for offset in range(2, 400):
            y, m = divmod(today.year * 12 + today.month - 1 + offset, 12)
            start = date(y, m + 1, 1)
            if start.isoformat() not in used:
                return start
        raise AssertionError("no free monthly period")

    def calculated_run(self) -> dict[str, Any]:
        """A new monthly run of the seeded employees, calculated (seed: all salaried)."""
        start = self._free_month()
        body = {"schedule": "monthly", "period_start": start.isoformat()}
        r = self.call("POST", "/v1/pay-runs", json=body)
        assert r.status_code == 201, (r.status_code, error_code(r))
        run_id = r.json()["id"]
        r = self.call("POST", f"/v1/pay-runs/{run_id}/calculate")
        assert r.status_code == 200, (r.status_code, error_code(r))
        run: dict[str, Any] = r.json()
        assert run["status"] == "calculated"
        assert run["employee_count"] > 0
        return run

    def approve(self, run_id: str) -> dict[str, Any]:
        r = self.call(
            "POST",
            f"/v1/pay-runs/{run_id}/approve",
            as_approver=True,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 200, (r.status_code, error_code(r))
        run: dict[str, Any] = r.json()
        assert run["status"] == "processing"
        return run

    def run(self, run_id: str) -> dict[str, Any]:
        r = self.call("GET", f"/v1/pay-runs/{run_id}")
        assert r.status_code == 200, (r.status_code, error_code(r))
        body: dict[str, Any] = r.json()
        return body

    def payouts(self, run_id: str) -> list[dict[str, Any]]:
        return self._pages(f"/v1/pay-runs/{run_id}/payouts")

    def wait_final(self, run_id: str, timeout: float) -> dict[str, Any]:
        def final() -> dict[str, Any] | None:
            run = self.run(run_id)
            return run if run["status"] in FINAL_RUN else None

        return wait_until(f"run {run_id} final", final, timeout, interval=2.0)

    def attempt_statuses(self, run_id: str) -> list[str]:
        return [p["attempts"][-1]["status"] for p in self.payouts(run_id) if p["attempts"]]

    def close(self) -> None:
        self.http.close()


# ------------------------------------------------------------------------------ assertions
def payout_ids(stack: Stack, run_id: str) -> list[str]:
    rows = stack.psql(
        f"SELECT id FROM payroll.payouts WHERE pay_run_id = '{uuid.UUID(run_id)}' "
        "AND NOT no_transfer ORDER BY id"
    )
    return [r[0] for r in rows]


def transfers(stack: Stack, ids: Sequence[str]) -> dict[str, list[tuple[str, str]]]:
    """Bank-stub ledger rows per payout: ``[(idempotency_key, status), ...]``. The bank key is
    ``<payout_id>:<attempt>`` (ADR 0003 a)."""
    if not ids:
        return {}
    keys = ",".join(f"'{uuid.UUID(i)}'" for i in ids)
    rows = stack.psql(
        "SELECT split_part(idempotency_key, ':', 1), idempotency_key, status "
        f"FROM bank_stub.transfers WHERE split_part(idempotency_key, ':', 1) IN ({keys}) "
        "ORDER BY created_at"
    )
    out: dict[str, list[tuple[str, str]]] = {i: [] for i in ids}
    for pid, key, status in rows:
        out[pid].append((key, status))
    return out


def assert_paid_once(stack: Stack, payroll: Payroll, run_id: str) -> None:
    """PAY-NFR-02: the run is paid, and every payout has exactly one settled bank transfer."""
    run = payroll.run(run_id)
    assert run["status"] == "paid", run["status"]
    payouts = payroll.payouts(run_id)
    assert payouts
    assert all(p["status"] == "paid" for p in payouts)
    ids = [p["id"] for p in payouts if not p["no_transfer"]]
    ledger = transfers(stack, ids)
    for pid in ids:
        settled = [k for k, s in ledger[pid] if s == "settled"]
        assert len(settled) == 1, f"payout {pid}: {ledger[pid]}"


def unsent_outbox(stack: Stack, run_id: str) -> int:
    rows = stack.psql(
        "SELECT count(*) FROM payroll.outbox o JOIN payroll.payouts p ON p.id = o.payout_id "
        f"WHERE p.pay_run_id = '{uuid.UUID(run_id)}' AND o.sent_at IS NULL"
    )
    return int(rows[0][0])


def publish_counts(stack: Stack, run_id: str) -> list[int]:
    rows = stack.psql(
        "SELECT a.publish_count FROM payroll.payout_attempts a "
        "JOIN payroll.payouts p ON p.id = a.payout_id "
        f"WHERE p.pay_run_id = '{uuid.UUID(run_id)}' AND a.attempt = p.current_attempt"
    )
    return [int(r[0]) for r in rows]


def disbursements(stack: Stack, ids: Sequence[str], status: str) -> int:
    """The worker's own record (payouts.disbursements) of attempts in ``status``."""
    if not ids:
        return 0
    keys = ",".join(f"'{uuid.UUID(i)}'" for i in ids)
    rows = stack.psql(
        f"SELECT count(*) FROM payouts.disbursements WHERE payout_id IN ({keys}) "
        f"AND status = '{status}'"
    )
    return int(rows[0][0])


def settled_count(stack: Stack, ids: Sequence[str]) -> int:
    return sum(1 for rows in transfers(stack, ids).values() for _, s in rows if s == "settled")


def log_events(text: str, event: str) -> int:
    """Occurrences of a structured log event in ``docker compose logs`` output."""
    return text.count(f'"event": "{event}"') + text.count(f'"event":"{event}"')


def log_records(text: str, event: str) -> list[dict[str, Any]]:
    """The JSON records of a structured log event in ``docker compose logs`` output (each line is
    ``<container> | {json}``); lines that aren't JSON are skipped."""
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            record = json.loads(line[start:])
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("event") == event:
            records.append(record)
    return records


# ------------------------------------------------------------------------------------- BFF
class Bff:
    """The web BFF on WEB_PORT, as a browser would use it (Origin header, session cookie)."""

    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.base = stack.url("WEB_PORT")
        self.http = httpx.Client(base_url=self.base, timeout=15)
        self.cookie_name = stack.env.get("SESSION_COOKIE_NAME", "sid")

    def call(
        self, method: str, path: str, *, cookie: str | None = None, **kw: Any
    ) -> httpx.Response:
        headers = {"Origin": self.base, **kw.pop("headers", {})}
        if cookie:
            headers["Cookie"] = f"{self.cookie_name}={cookie}"
        return self.http.request(method, path, headers=headers, **kw)

    def login_response(
        self, email: str | None = None, password: str | None = None
    ) -> httpx.Response:
        body = {
            "email": email or self.stack.env["SEED_OWNER_EMAIL"],
            "password": password or self.stack.env["SEED_OWNER_PASSWORD"],
        }
        return self.call("POST", "/api/auth/login", json=body)

    def login(self, timeout: float = 60) -> str:
        """Signs the seeded owner in; returns the session cookie value. Waits out a 503 while the
        BFF's identity breaker is still open after an earlier fault (BREAKER_RESET_S = 30 s)."""

        def answered() -> httpx.Response | None:
            x = self.login_response()
            return None if x.status_code == 503 else x

        r = wait_until("owner sign-in", answered, timeout)
        assert r.status_code == 200, (r.status_code, error_code(r))
        for h in r.headers.get_list("set-cookie"):  # Secure cookie over http: read it ourselves
            if h.startswith(f"{self.cookie_name}="):
                return h.split(";", 1)[0].split("=", 1)[1]
        raise AssertionError("no session cookie")

    def close(self) -> None:
        self.http.close()

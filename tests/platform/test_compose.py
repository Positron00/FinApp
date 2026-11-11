"""Docker Compose files (platform LLD §6.2, §7; PAY-NFR-13, C1, C4, C23, C53, C65, R16).

``docker compose config`` renders deploy/compose/compose.yaml alone and with compose.test.yaml
against a throwaway .env written by scripts/dev-env.sh at a non-default APP_PORT_BASE, so the
port offsets are checked, not the defaults. The raw YAML is checked too: no credential is written
inline, every one comes from .env through ``${VAR:?}``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
import yaml  # type: ignore[import-untyped]
from dotenv import dotenv_values
from pycommon.testing import find_repo_root

ROOT: Final = find_repo_root()
COMPOSE_DIR: Final = ROOT / "deploy" / "compose"
BASE_FILE: Final = COMPOSE_DIR / "compose.yaml"
TEST_FILE: Final = COMPOSE_DIR / "compose.test.yaml"
PORT_BASE: Final = 5200  # not the default 4100: proves the offsets follow APP_PORT_BASE

# §7.1: service -> profile.
PROFILES: Final = {
    **dict.fromkeys(
        [
            "postgres", "redis", "mailpit", "migrate-identity", "migrate-payroll",
            "migrate-payouts", "migrate-bank-stub", "bootstrap-owner", "identity", "payroll",
            "bank-stub", "payout-worker", "web",
        ],
        "core",
    ),
    **dict.fromkeys(
        [
            "otel-collector", "tempo", "loki", "prometheus", "alertmanager", "alert-sink",
            "grafana", "postgres-exporter", "redis-exporter",
        ],
        "obs",
    ),
    "toxiproxy": "test",
}  # fmt: skip
ONE_SHOTS: Final = {
    "migrate-identity", "migrate-payroll", "migrate-payouts", "migrate-bank-stub",
    "bootstrap-owner",
}  # fmt: skip
PY_SERVICES: Final = {"identity", "payroll", "payout-worker", "bank-stub"}
APP_SERVICES: Final = PY_SERVICES | {"web"}
# No healthcheck by design: the collector image has no shell or HTTP client; the exporters are
# watched through their scrape `up`; Tempo and Loki are only `service_started` dependencies of
# Grafana (their distroless images have no wget). Everything else must have one.
NO_HEALTHCHECK: Final = {"otel-collector", "tempo", "loki", "postgres-exporter", "redis-exporter"}

# Host ports (§7.1): key -> (service, container port, APP_PORT_BASE offset).
BASE_PORTS: Final = {
    "WEB_PORT": ("web", 3000, 0),
    "GRAFANA_PORT": ("grafana", 3000, 1),
    "MAILPIT_PORT": ("mailpit", 8025, 2),
}
TEST_PORTS: Final = {
    **BASE_PORTS,
    "IDENTITY_PORT": ("identity", 8000, 10),
    "PAYROLL_PORT": ("payroll", 8000, 11),
    "PAYOUT_WORKER_PORT": ("payout-worker", 8000, 12),
    "BANK_STUB_PORT": ("bank-stub", 8000, 13),
    "TEMPO_PORT": ("tempo", 3200, 30),
    "LOKI_PORT": ("loki", 3100, 31),
    "PROMETHEUS_PORT": ("prometheus", 9090, 32),
    "ALERTMANAGER_PORT": ("alertmanager", 9093, 33),
    "TOXIPROXY_PORT": ("toxiproxy", 8474, 34),
    "ALERT_SINK_PORT": ("alert-sink", 8080, 35),
}

MIGRATION_OF: Final = {
    "identity": "migrate-identity",
    "payroll": "migrate-payroll",
    "payout-worker": "migrate-payouts",
    "bank-stub": "migrate-bank-stub",
    "bootstrap-owner": "migrate-identity",
}
DB_USER: Final = {  # C2: <schema>_app at runtime, <schema>_migrator for one-shots
    "identity": "identity_app",
    "payroll": "payroll_app",
    "payout-worker": "payouts_app",
    "bank-stub": "bank_stub_app",
    "migrate-identity": "identity_migrator",
    "migrate-payroll": "payroll_migrator",
    "migrate-payouts": "payouts_migrator",
    "migrate-bank-stub": "bank_stub_migrator",
    "bootstrap-owner": "identity_migrator",
}
REDIS_CLIENTS: Final = {"identity", "payroll", "payout-worker", "web"}
PAYOUT_TIMING: Final = {  # x-payout-timing: production values in both files (C53)
    "PAYOUT_MAX_DELIVERIES": "5",
    "PAYOUT_CLAIM_MIN_IDLE_MS": "60000",
    "PAYOUT_REPUBLISH_AFTER_S": "360",
    "PAYOUT_REPUBLISH_MAX_BACKOFF_S": "900",
    "PAYOUT_REPUBLISH_MAX": "10",
    "BANK_TOTAL_DEADLINE_S": "15",
    "PAYOUT_PAYROLL_CALL_DEADLINE_S": "5",
    "PAYOUT_MARK_REJECTED_MIN_WAIT_S": "120",
}
SECRET_KEY: Final = re.compile(r"(PASSWORD|SECRET|PRIVATE_KEY|API_KEY|FIELD_KEYS|HASH_KEY|PASS)$")
FROM_ENV: Final = re.compile(r"^\$\{[A-Z0-9_]+:\?[^}]*\}$")
VIA_TOXIPROXY: Final = {"DB_HOST": "toxiproxy", "DB_PORT": "15432"}
REDIS_VIA_TOXIPROXY: Final = {"REDIS_HOST": "toxiproxy", "REDIS_PORT": "16379"}

Config = dict[str, Any]


@pytest.fixture(scope="module")
def env_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    path = tmp_path_factory.mktemp("compose") / ".env"
    path.write_text(f"APP_PORT_BASE={PORT_BASE}\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "dev-env.sh"), "--env-file", str(path)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    if result.returncode == 1 and "OpenSSL 3 is required" in result.stderr:
        pytest.skip("OpenSSL 3 is not installed")
    assert result.returncode == 0, result.stderr
    return path


@pytest.fixture(scope="module")
def dotenv(env_file: Path) -> dict[str, str]:
    return {k: v for k, v in dotenv_values(env_file).items() if v is not None}


def _config(env_file: Path, *, test: bool, profiles: tuple[str, ...]) -> Config:
    cmd = ["docker", "compose", "--env-file", str(env_file), "-f", str(BASE_FILE)]
    if test:
        cmd += ["-f", str(TEST_FILE)]
    for p in profiles:
        cmd += ["--profile", p]
    result = subprocess.run(
        [*cmd, "config", "--format", "json"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
    cfg: Config = json.loads(result.stdout)
    return cfg


@pytest.fixture(scope="module")
def base(env_file: Path) -> Config:
    return _config(env_file, test=False, profiles=("core", "obs"))


@pytest.fixture(scope="module")
def full(env_file: Path) -> Config:
    """What scripts/stack.sh up runs by default (core, obs, test with the override)."""
    return _config(env_file, test=True, profiles=("core", "obs", "test"))


def _env(cfg: Config, svc: str) -> dict[str, str]:
    return {k: str(v) for k, v in (cfg["services"][svc].get("environment") or {}).items()}


def _published(cfg: Config) -> Iterator[tuple[str, dict[str, Any]]]:
    for name, svc in cfg["services"].items():
        for port in svc.get("ports") or []:
            yield name, port


def _raw(path: Path) -> Config:
    data: Config = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


# ------------------------------------------------------------------------------- validity, profiles
@pytest.mark.parametrize(
    ("test", "profiles"),
    [
        (False, ("core",)),
        (False, ("core", "obs")),
        (True, ("core", "test")),
        (True, ("core", "obs", "test")),
    ],
)
def test_config_is_valid_for_every_profile_set(
    env_file: Path, test: bool, profiles: tuple[str, ...]
) -> None:
    """PAY-NFR-13: each profile set stack.sh can start renders (no core service depends on obs,
    so core alone is valid; the test profile needs the override)."""
    cfg = _config(env_file, test=test, profiles=profiles)
    expected = {s for s, p in PROFILES.items() if p in profiles}
    assert set(cfg["services"]) == expected


def test_every_service_has_its_profile(full: Config) -> None:
    """PAY-NFR-13, §7.1: core, obs and test hold exactly the documented services."""
    assert {s: svc["profiles"] for s, svc in full["services"].items()} == {
        s: [p] for s, p in PROFILES.items()
    }


def test_no_core_service_depends_on_an_obs_service(full: Config) -> None:
    """PAY-NFR-13, §7.1: telemetry loss never blocks the app."""
    for name, svc in full["services"].items():
        if PROFILES[name] != "core":
            continue
        deps = set(svc.get("depends_on") or {})
        assert not {d for d in deps if PROFILES.get(d) == "obs"}, name


# ----------------------------------------------------------------------------------------- ports
@pytest.mark.parametrize(("which", "expected"), [("base", BASE_PORTS), ("full", TEST_PORTS)])
def test_ports_bind_localhost_at_the_base_offsets(
    request: pytest.FixtureRequest, which: str, expected: dict[str, tuple[str, int, int]]
) -> None:
    """PAY-NFR-13, §7.1: every published port binds 127.0.0.1 at APP_PORT_BASE + its offset;
    the base file publishes only the web, Grafana and Mailpit, the override adds the rest."""
    cfg: Config = request.getfixturevalue(which)
    got = {
        (name, int(p["target"])): (p.get("host_ip"), int(p["published"]))
        for name, p in _published(cfg)
    }
    want = {(svc, target): ("127.0.0.1", PORT_BASE + off) for svc, target, off in expected.values()}
    assert got == want


def test_port_keys_in_env_match_the_offsets(dotenv: dict[str, str]) -> None:
    """PAY-NFR-13: dev-env.sh derives every port key from APP_PORT_BASE."""
    assert {k: int(dotenv[k]) for k in TEST_PORTS} == {
        k: PORT_BASE + off for k, (_, _, off) in TEST_PORTS.items()
    }


# ------------------------------------------------------------------------- health, dependencies
def test_long_running_services_have_healthchecks(full: Config) -> None:
    """PAY-NFR-13, §7.1: every long-running service but the documented exceptions has a
    healthcheck; one-shots don't restart."""
    for name, svc in full["services"].items():
        if name in ONE_SHOTS:
            assert svc.get("restart") == "no", name
            continue
        assert svc.get("restart") == "unless-stopped", name
        if name in NO_HEALTHCHECK:
            continue
        hc = svc.get("healthcheck")
        assert hc, name
        assert hc.get("test"), name
        assert not hc.get("disable"), name


def test_python_services_probe_readyz(full: Config) -> None:
    """PAY-605: the Python services' healthcheck is GET /readyz on port 8000."""
    for name in PY_SERVICES:
        test = " ".join(full["services"][name]["healthcheck"]["test"])
        assert "http://127.0.0.1:8000/readyz" in test, name


def test_services_wait_for_their_migration(full: Config) -> None:
    """PAY-NFR-13: each service (and bootstrap-owner) starts after its migration completed."""
    for svc, migration in MIGRATION_OF.items():
        deps = full["services"][svc]["depends_on"]
        assert deps[migration]["condition"] == "service_completed_successfully", svc
    web = full["services"]["web"]["depends_on"]
    assert web["bootstrap-owner"]["condition"] == "service_completed_successfully"
    for dep in ("identity", "payroll", "redis"):
        assert web[dep]["condition"] == "service_healthy"


def test_web_contract(full: Config) -> None:
    """C4: the web listens on PORT=3000, publishes ${WEB_PORT}:3000 and probes /api/readyz."""
    web = full["services"]["web"]
    assert _env(full, "web")["PORT"] == "3000"
    assert [int(p["target"]) for p in web["ports"]] == [3000]
    assert "/api/readyz" in " ".join(web["healthcheck"]["test"])


def test_bootstrap_owner_runs_if_empty(full: Config) -> None:
    """R16, PAY-609: bootstrap-owner is idempotent (--if-empty), with no exit-code wrapper."""
    cmd = full["services"]["bootstrap-owner"]["command"]
    assert cmd[:3] == ["python", "-m", "identity.bootstrap_owner"]
    assert "--if-empty" in cmd


def test_app_containers_are_hardened(full: Config) -> None:
    """PAY-NFR-13, §7.1: app containers match Kubernetes (read-only root, no capabilities)."""
    for name in APP_SERVICES | ONE_SHOTS | {"toxiproxy"}:
        svc = full["services"][name]
        assert svc.get("read_only") is True, name
        assert svc.get("cap_drop") == ["ALL"], name
        assert "no-new-privileges:true" in svc.get("security_opt", []), name


# ----------------------------------------------------------------------------- env contract (C1)
@pytest.mark.parametrize("which", ["base", "full"])
def test_env_contract(request: pytest.FixtureRequest, which: str) -> None:
    """C1, C2, C12: no URL-style DB or Redis keys; every Python service and one-shot has the
    DB_* keys with its own role; every Redis client has the REDIS_* keys."""
    cfg: Config = request.getfixturevalue(which)
    for name in cfg["services"]:
        env = _env(cfg, name)
        assert not {"DATABASE_URL", "MIGRATOR_DATABASE_URL", "REDIS_URL"} & set(env), name
    for name, user in DB_USER.items():
        env = _env(cfg, name)
        for key in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SCHEMA"):
            assert env.get(key), f"{name}: {key}"
        assert env["DB_USER"] == user, name
    for name in REDIS_CLIENTS:
        env = _env(cfg, name)
        for key in ("REDIS_HOST", "REDIS_PORT", "REDIS_USERNAME", "REDIS_PASSWORD"):
            assert env.get(key), f"{name}: {key}"


def test_traces_sampler_everywhere(full: Config) -> None:
    """C23: every app service samples parentbased_always_on with no _ARG."""
    for name in APP_SERVICES | ONE_SHOTS:
        env = _env(full, name)
        assert env.get("OTEL_TRACES_SAMPLER") == "parentbased_always_on", name
        assert "OTEL_TRACES_SAMPLER_ARG" not in env, name


# --------------------------------------------------------------------------------------- secrets
@pytest.mark.parametrize("path", [BASE_FILE, TEST_FILE], ids=lambda p: p.name)
def test_no_secret_is_inline(path: Path) -> None:
    """PAY-NFR-13, ADR 0008: every credential in the YAML is a required ${VAR:?} from .env."""
    raw = _raw(path)
    blocks: list[tuple[str, dict[str, Any]]] = [
        (k, v) for k, v in raw.items() if k.startswith("x-") and isinstance(v, dict)
    ]
    for name, svc in (raw.get("services") or {}).items():
        env = svc.get("environment")
        if isinstance(env, dict):
            blocks.append((name, env))
    checked = 0
    for name, block in blocks:
        for key, value in block.items():
            if isinstance(key, str) and SECRET_KEY.search(key):
                checked += 1
                assert isinstance(value, str), f"{name}: {key}"
                assert FROM_ENV.match(value), f"{name}: {key} is not a required ${{VAR:?}}"
    if path == BASE_FILE:
        assert checked >= 20  # the guard itself found the credentials


def test_rendered_secrets_come_from_env(full: Config, dotenv: dict[str, str]) -> None:
    """ADR 0008: every rendered credential equals a .env value (none is a literal)."""
    values = set(dotenv.values())
    for name in full["services"]:
        for key, value in _env(full, name).items():
            if SECRET_KEY.search(key):
                assert value in values, f"{name}: {key}"


# -------------------------------------------------------------------------- compose.test.yaml
def test_override_routes_runtime_traffic_through_toxiproxy(base: Config, full: Config) -> None:
    """Platform LLD §7.4: runtime DB and Redis traffic (and worker -> payroll, worker -> bank,
    web -> payroll) goes through Toxiproxy with the test profile; base traffic is direct."""
    for name in ("identity", "payroll", "payout-worker"):
        env = _env(full, name)
        assert {k: env[k] for k in (*VIA_TOXIPROXY, *REDIS_VIA_TOXIPROXY)} == {
            **VIA_TOXIPROXY,
            **REDIS_VIA_TOXIPROXY,
        }, name
        assert full["services"][name]["depends_on"]["toxiproxy"]["condition"] == "service_healthy"
    bank = _env(full, "bank-stub")
    assert {k: bank[k] for k in VIA_TOXIPROXY} == VIA_TOXIPROXY
    worker = _env(full, "payout-worker")
    assert worker["PAYOUT_PAYROLL_BASE_URL"] == "http://toxiproxy:18001"
    assert worker["BANK_BASE_URL"] == "http://toxiproxy:18002"
    web = _env(full, "web")
    assert web["PAYROLL_BASE_URL"] == "http://toxiproxy:18001"
    assert web["IDENTITY_BASE_URL"] == "http://identity:8000"  # identity-down stops the container
    assert {k: web[k] for k in REDIS_VIA_TOXIPROXY} == REDIS_VIA_TOXIPROXY
    for name in APP_SERVICES:
        env = _env(base, name)
        assert "toxiproxy" not in " ".join(env.values()), name


def test_override_keeps_start_up_and_monitoring_direct(full: Config) -> None:
    """Platform LLD §7.4: migrations, bootstrap-owner and the exporters never go through
    Toxiproxy, so a toxic can't break start-up or monitoring."""
    for name in ONE_SHOTS:
        env = _env(full, name)
        assert (env["DB_HOST"], env["DB_PORT"]) == ("postgres", "5432"), name
    assert _env(full, "postgres-exporter")["DATA_SOURCE_URI"].startswith("postgres:5432/")
    assert _env(full, "redis-exporter")["REDIS_ADDR"] == "redis://redis:6379"
    toxi = full["services"]["toxiproxy"]
    assert {d: c["condition"] for d, c in toxi["depends_on"].items()} == {
        "postgres": "service_healthy",
        "redis": "service_healthy",
    }


def test_override_raises_only_the_rate_limits(base: Config, full: Config) -> None:
    """Plan §12, C53, C65: the test profile raises the rate limits and makes bank-stub
    deterministic; payout timing keeps its production values in both files."""
    for name in ("identity", "payroll"):
        assert _env(base, name)["SERVICE_RATE_LIMIT_USER"] == "600"
        assert _env(full, name)["SERVICE_RATE_LIMIT_USER"] == "1000000"
    ident = _env(full, "identity")
    assert (ident["AUTH_LOGIN_LIMIT_ACCOUNT"], ident["AUTH_LOGIN_LIMIT_IP"]) == ("1000000",) * 2
    web_base, web_test = _env(base, "web"), _env(full, "web")
    assert (web_base["BFF_RATE_LIMIT_SESSION"], web_base["BFF_RATE_LIMIT_IP"]) == ("120", "300")
    assert (web_test["BFF_RATE_LIMIT_SESSION"], web_test["BFF_RATE_LIMIT_IP"]) == ("1000000",) * 2
    assert _env(base, "bank-stub")["BANK_STUB_FAILURE_RATE"] == "0.05"
    assert _env(full, "bank-stub")["BANK_STUB_FAILURE_RATE"] == "0"
    for cfg in (base, full):
        for name in ("payroll", "payout-worker"):
            env = _env(cfg, name)
            assert {k: env[k] for k in PAYOUT_TIMING} == PAYOUT_TIMING, name


def test_override_changes_nothing_else(base: Config, full: Config) -> None:
    """Platform LLD §7.4: outside the documented keys, every service's environment is the same
    with and without the override (so tests exercise the production configuration)."""
    allowed = {
        *VIA_TOXIPROXY, *REDIS_VIA_TOXIPROXY, "SERVICE_RATE_LIMIT_USER",
        "SERVICE_RATE_LIMIT_WORKER", "AUTH_LOGIN_LIMIT_ACCOUNT", "AUTH_LOGIN_LIMIT_IP",
        "PAYOUT_PAYROLL_BASE_URL", "BANK_BASE_URL", "PAYROLL_BASE_URL", "BANK_STUB_FAILURE_RATE",
        "BFF_RATE_LIMIT_SESSION", "BFF_RATE_LIMIT_IP",
    }  # fmt: skip
    for name in base["services"]:
        b, f = _env(base, name), _env(full, name)
        changed = {k for k in set(b) | set(f) if b.get(k) != f.get(k)}
        assert changed <= allowed, f"{name}: {sorted(changed - allowed)}"

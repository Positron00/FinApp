"""scripts/dev-env.sh: a complete, idempotent dev .env (platform LLD §3.15; PAY-600, C23, C28)."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dotenv import dotenv_values
from pycommon.auth import Keyset
from pycommon.settings import normalise_pem
from pycommon.testing import find_repo_root

ROOT = find_repo_root()
SCRIPT = ROOT / "scripts" / "dev-env.sh"
COMPANY_ID = "0193a000-0000-7000-8000-000000000001"  # C28

KEPT = [
    "ENV", "COMPOSE_PROJECT_NAME", "APP_PORT_BASE", "COMPANY_ID", "POSTGRES_PASSWORD",
    "IDENTITY_DB_MIGRATOR_PASSWORD", "IDENTITY_DB_PASSWORD", "PAYROLL_DB_MIGRATOR_PASSWORD",
    "PAYROLL_DB_PASSWORD", "PAYOUTS_DB_MIGRATOR_PASSWORD", "PAYOUTS_DB_PASSWORD",
    "BANK_STUB_DB_MIGRATOR_PASSWORD", "BANK_STUB_DB_PASSWORD", "POSTGRES_EXPORTER_PASSWORD",
    "REDIS_ADMIN_PASSWORD", "REDIS_WEB_PASSWORD", "REDIS_IDENTITY_PASSWORD",
    "REDIS_PAYROLL_PASSWORD", "REDIS_PAYOUT_WORKER_PASSWORD", "REDIS_EXPORTER_PASSWORD",
    "INTERNAL_JWT_PRIVATE_KEY", "INTERNAL_JWT_KID", "PAYOUT_WORKER_JWT_PRIVATE_KEY",
    "PAYOUT_WORKER_JWT_KID", "PAYROLL_FIELD_KEYS", "PAYROLL_FIELD_ACTIVE_KID", "AUDIT_IP_HASH_KEY",
    "SESSION_SECRET", "BANK_STUB_API_KEY", "GRAFANA_ADMIN_PASSWORD", "SMTP_HOST", "SMTP_PORT",
    "ALERT_PAGE_WEBHOOK_URL", "ALERT_TICKET_WEBHOOK_URL", "OTEL_TRACES_SAMPLER",
    "SEED_OWNER_EMAIL", "SEED_OWNER_PASSWORD", "SEED_APPROVER_PASSWORD", "LOAD_USER_EMAIL",
    "LOAD_USER_PASSWORD",
]  # fmt: skip
PORTS = {
    "WEB_PORT": 0, "GRAFANA_PORT": 1, "MAILPIT_PORT": 2, "IDENTITY_PORT": 10, "PAYROLL_PORT": 11,
    "PAYOUT_WORKER_PORT": 12, "BANK_STUB_PORT": 13, "TEMPO_PORT": 30, "LOKI_PORT": 31,
    "PROMETHEUS_PORT": 32, "ALERTMANAGER_PORT": 33, "TOXIPROXY_PORT": 34, "ALERT_SINK_PORT": 35,
}  # fmt: skip
DERIVED = [
    "APP_VERSION",
    *PORTS,
    "IDENTITY_INTERNAL_JWT_PUBLIC_KEYS",
    "PAYROLL_INTERNAL_JWT_PUBLIC_KEYS",
]


def _run(env_file: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--env-file", str(env_file), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd or ROOT,
    )
    if result.returncode == 1 and "OpenSSL 3 is required" in result.stderr:
        pytest.skip("OpenSSL 3 is not installed")
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    _run(path)
    return path


def _values(path: Path) -> dict[str, str]:
    return {k: v or "" for k, v in dotenv_values(path).items()}


def _public_pem(private_pem: str) -> bytes:
    key = serialization.load_pem_private_key(normalise_pem(private_pem).encode(), None)
    assert isinstance(key, Ed25519PrivateKey)
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def test_every_key_is_written_with_mode_0600(env_file: Path) -> None:
    """PAY-600, platform §3.15: every listed key, the dev values, and a private file."""
    values = _values(env_file)
    assert set(KEPT) | set(DERIVED) <= set(values)
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert values["ENV"] == "dev"
    assert values["SMTP_HOST"] == "mailpit"
    assert values["SMTP_PORT"] == "1025"
    assert "SMTP_USERNAME" not in values  # no credentials in dev (C49)
    assert values["SEED_OWNER_EMAIL"] == "owner@payrun.example.com"  # C59
    assert values["LOAD_USER_EMAIL"] == "load@payrun.example.com"
    for key in KEPT:
        if key.endswith("_PASSWORD") and key.startswith(("POSTGRES", "REDIS_", "GRAFANA")):
            assert len(values[key]) == 48
            int(values[key], 16)  # hex: no escaping in ACL lines or URLs


def test_sampler_and_company_id(env_file: Path) -> None:
    """C23, C28: parentbased_always_on with no _ARG; COMPANY_ID equals every event example's."""
    values = _values(env_file)
    assert values["OTEL_TRACES_SAMPLER"] == "parentbased_always_on"
    assert "OTEL_TRACES_SAMPLER_ARG" not in values
    assert values["COMPANY_ID"] == COMPANY_ID
    examples = sorted((ROOT / "contracts" / "events" / "examples").glob("*.json"))
    assert examples
    for path in examples:
        doc = json.loads(path.read_text())
        envelope = doc.get("envelope", doc)
        if isinstance(envelope, str):
            envelope = json.loads(envelope)
        assert envelope["data"]["company_id"] == COMPANY_ID, path.name


def test_keysets_match_the_private_keys_and_kids_are_dev(env_file: Path) -> None:
    """PAY-600: identity trusts the web key; payroll trusts web and worker; kids start with dev-."""
    values = _values(env_file)
    web_kid, worker_kid = values["INTERNAL_JWT_KID"], values["PAYOUT_WORKER_JWT_KID"]
    assert web_kid.startswith("dev-web-")
    assert worker_kid.startswith("dev-worker-")
    identity = Keyset.from_json(values["IDENTITY_INTERNAL_JWT_PUBLIC_KEYS"])
    payroll = Keyset.from_json(values["PAYROLL_INTERNAL_JWT_PUBLIC_KEYS"])
    assert identity.kids == {web_kid}
    assert payroll.kids == {web_kid, worker_kid}
    raw = serialization.Encoding.Raw, serialization.PublicFormat.Raw
    web = payroll.get(web_kid)
    worker = payroll.get(worker_kid)
    assert web is not None
    assert worker is not None
    assert (web.iss, worker.iss) == ("web", "payout-worker")
    assert web.public_key.public_bytes(*raw) == _public_pem(values["INTERNAL_JWT_PRIVATE_KEY"])
    assert worker.public_key.public_bytes(*raw) == _public_pem(
        values["PAYOUT_WORKER_JWT_PRIVATE_KEY"]
    )
    field_keys = json.loads(values["PAYROLL_FIELD_KEYS"])
    assert values["PAYROLL_FIELD_ACTIVE_KID"] in field_keys


def test_second_run_keeps_values_and_rederives_ports(env_file: Path) -> None:
    """Platform §3.15: kept keys survive a re-run; ports follow a changed APP_PORT_BASE."""
    before = _values(env_file)
    text = env_file.read_text().replace("APP_PORT_BASE=4100", "APP_PORT_BASE=5200")
    env_file.write_text(text)
    result = _run(env_file)
    assert "complete" in result.stdout
    after = _values(env_file)
    for key in KEPT:
        if key != "APP_PORT_BASE":
            assert after[key] == before[key], key
    for key, offset in PORTS.items():
        assert after[key] == str(5200 + offset)
    lines = [ln.split("=", 1)[0] for ln in env_file.read_text().splitlines() if ln]
    assert len(lines) == len(set(lines))  # derived keys replaced, never duplicated


def test_output_names_keys_but_never_values(tmp_path: Path) -> None:
    """PAY-NFR-07: the script prints the names of the keys it added, never a value."""
    path = tmp_path / ".env"
    result = _run(path)
    values = _values(path)
    assert "SESSION_SECRET" in result.stdout
    for key in KEPT:
        if key.endswith(("_PASSWORD", "_KEY", "_SECRET", "_KEYS")):
            assert values[key] not in result.stdout + result.stderr


def test_k8s_dev_file_is_unquoted_and_parses(tmp_path: Path) -> None:
    """Platform §3.15: the secretGenerator env file has unquoted single-line values."""
    env_path = tmp_path / ".env"
    k8s_path = tmp_path / "k8s" / "secrets.env"
    _run(env_path, "--k8s-dev", "--k8s-dev-file", str(k8s_path), cwd=tmp_path)
    assert stat.S_IMODE(k8s_path.stat().st_mode) == 0o600
    pairs = dict(ln.split("=", 1) for ln in k8s_path.read_text().splitlines())
    assert "WEB_PORT" not in pairs
    assert "APP_PORT_BASE" not in pairs
    assert pairs["SMTP_PORT"] == "1025"
    for key, value in pairs.items():
        assert value[:1] not in {"'", '"'}, key
    assert Keyset.from_json(pairs["PAYROLL_INTERNAL_JWT_PUBLIC_KEYS"]).kids
    json.loads(pairs["PAYROLL_FIELD_KEYS"])
    assert "\\n" in pairs["INTERNAL_JWT_PRIVATE_KEY"]
    _public_pem(pairs["INTERNAL_JWT_PRIVATE_KEY"])

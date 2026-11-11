"""scripts/seed.py against the running stack (platform LLD §3.15, §6.2; PAY-609, C27, C33, C54).

Marker ``stack``: run by ``scripts/ci/e2e.sh`` after Playwright, with ``ci-logs/seed.json`` (or
``SEED_JSON``) written by the seed. Skipped when the stack or the summary is missing; the
``platform`` CI member ignores this file.

- the JSON summary validates (keys and types, ``company_id`` = ``COMPANY_ID``);
- a re-run is a no-op: two runs in a row print the same IDs and counts;
- ``ENV=uat`` exits 2 before any request;
- ``LOAD_USER_EMAIL`` has exactly ``[payroll_admin]`` (C27);
- skeleton mode: 5 salaried monthly employees; full mode: the R22 settings and both schedules
  and pay types. In both, ``runs.current`` is a calculated (or later) current-month monthly run
  and ``runs.draft`` a draft next-month one (C33, C54).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.mint_token import load_env  # noqa: E402  (repo root added just above)
from scripts.seed import month_start  # noqa: E402

pytestmark = pytest.mark.stack

SUMMARY_KEYS = {"mode", "company_id", "users", "employees", "runs"}
PAST_CALCULATED = {"calculated", "processing", "paid", "partially_failed"}


@pytest.fixture(scope="module")
def env() -> dict[str, str]:
    return load_env(REPO_ROOT / ".env")


@pytest.fixture(scope="module")
def summary_path(env: dict[str, str]) -> Path:
    path = Path(env.get("SEED_JSON") or REPO_ROOT / "ci-logs" / "seed.json")
    if not path.is_file():
        pytest.skip(f"no seed summary at {path} (run scripts/seed.py --out ci-logs/seed.json)")
    try:
        r = httpx.get(f"http://127.0.0.1:{env['WEB_PORT']}/api/readyz", timeout=3)
    except httpx.HTTPError, KeyError:
        pytest.skip("the stack is not running (scripts/stack.sh up)")
    if r.status_code != 200:
        pytest.skip("the stack is not ready")
    return path


@pytest.fixture(scope="module")
def summary(summary_path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(summary_path.read_text(encoding="utf-8"))
    return data


def seed_args(summary: dict[str, Any]) -> list[str]:
    """The arguments that reproduce the summary's mode."""
    if summary["mode"] == "skeleton":
        return ["--skeleton"]
    return ["--employees", "500"] if summary["employees"] >= 500 else []


def run_seed(args: list[str], **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "seed.py"), *args],
        cwd=REPO_ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


class Bff:
    def __init__(self, env: dict[str, str]) -> None:
        self.base = f"http://127.0.0.1:{env['WEB_PORT']}"
        self.cookie_name = env.get("SESSION_COOKIE_NAME", "sid")
        self.http = httpx.Client(base_url=self.base, timeout=20)

    def login(self, email: str, password: str) -> str:
        r = self.http.post(
            "/api/auth/login",
            json={"email": email, "password": password},
            headers={"Origin": self.base},
        )
        assert r.status_code == 200, f"login {email}: HTTP {r.status_code}"
        for h in r.headers.get_list("set-cookie"):
            if h.startswith(f"{self.cookie_name}="):
                return h.split(";", 1)[0]
        raise AssertionError("no session cookie")

    def get(self, cookie: str, path: str, **params: Any) -> Any:
        r = self.http.get(path, headers={"Cookie": cookie}, params=params)
        assert r.status_code == 200, f"GET {path}: HTTP {r.status_code}"
        return r.json()

    def all(self, cookie: str, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = self.get(cookie, path, limit=100, **({"cursor": cursor} if cursor else {}))
            items.extend(page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                return items


@pytest.fixture(scope="module")
def bff(env: dict[str, str], summary_path: Path) -> Iterator[Bff]:
    client = Bff(env)
    yield client
    client.http.close()


@pytest.fixture(scope="module")
def owner(bff: Bff, env: dict[str, str]) -> str:
    return bff.login(env["SEED_OWNER_EMAIL"], env["SEED_OWNER_PASSWORD"])


def test_summary_validates(summary: dict[str, Any], env: dict[str, str]) -> None:
    assert set(summary) == SUMMARY_KEYS
    assert summary["mode"] in {"skeleton", "full"}
    assert summary["company_id"] == env["COMPANY_ID"]
    assert set(summary["users"]) == {"owner", "approver", "load"}
    for uid in summary["users"].values():
        UUID(uid)
    assert isinstance(summary["employees"], int)
    assert summary["employees"] >= (5 if summary["mode"] == "skeleton" else 10)
    assert set(summary["runs"]) == {"current", "draft"}
    for rid in summary["runs"].values():
        UUID(rid)


def test_rerun_is_a_no_op(summary: dict[str, Any]) -> None:
    """Two runs in a row print the same IDs and counts, and both match the stored summary's
    users and runs (the employee count may have grown: the e2e specs add employees)."""
    first = run_seed(seed_args(summary))
    assert first.returncode == 0, first.stderr[-2000:]
    second = run_seed(seed_args(summary))
    assert second.returncode == 0, second.stderr[-2000:]
    a, b = json.loads(first.stdout), json.loads(second.stdout)
    assert a == b
    assert a["users"] == summary["users"]
    assert a["runs"] == summary["runs"]
    assert "created" not in second.stderr.replace("created=0", "")
    assert "settings set" not in second.stderr


def test_refuses_outside_dev() -> None:
    r = run_seed([], ENV="uat")
    assert r.returncode == 2
    assert r.stdout == ""
    assert "ENV is not dev" in r.stderr


def test_bad_arguments_exit_2() -> None:
    assert run_seed(["--skeleton", "--employees", "10"]).returncode == 2
    assert run_seed(["--employees", "7"]).returncode == 2


def test_load_user_is_payroll_admin_only(bff: Bff, env: dict[str, str]) -> None:
    cookie = bff.login(env["LOAD_USER_EMAIL"], env["LOAD_USER_PASSWORD"])
    me = bff.get(cookie, "/api/auth/me")
    assert me["roles"] == ["payroll_admin"]


def test_approver_is_approver_only(bff: Bff, env: dict[str, str], summary: dict[str, Any]) -> None:
    cookie = bff.login("approver@payrun.example.com", env["SEED_APPROVER_PASSWORD"])
    me = bff.get(cookie, "/api/auth/me")
    assert me["roles"] == ["approver"]
    assert me["id"] == summary["users"]["approver"]


def test_runs(bff: Bff, owner: str, summary: dict[str, Any]) -> None:
    today = datetime.now(UTC).date()
    current = bff.get(owner, f"/api/pay-runs/{summary['runs']['current']}")
    draft = bff.get(owner, f"/api/pay-runs/{summary['runs']['draft']}")
    assert current["schedule"] == draft["schedule"] == "monthly"
    assert current["period_start"] == month_start(today).isoformat()
    assert draft["period_start"] == month_start(today, 1).isoformat()
    assert draft["status"] == "draft"
    if summary["mode"] == "skeleton":
        assert current["status"] == "calculated"
    else:
        assert current["status"] in PAST_CALCULATED


def test_employees_and_settings(bff: Bff, owner: str, summary: dict[str, Any]) -> None:
    employees = bff.all(owner, "/api/employees")
    seeded = [e for e in employees if e["email"].startswith("employee")]
    settings = bff.get(owner, "/api/settings")
    if summary["mode"] == "skeleton":
        assert len(seeded) == 5
        assert {(e["pay_type"], e["schedule"]) for e in seeded} == {("salaried", "monthly")}
        return
    assert len(seeded) == (500 if summary["employees"] >= 500 else 10)
    shapes = {(e["pay_type"], e["schedule"]) for e in seeded}
    assert shapes == {
        ("salaried", "monthly"),
        ("hourly", "monthly"),
        ("salaried", "biweekly"),
        ("hourly", "biweekly"),
    }
    assert settings["tax_rate_bps"] == 2000
    assert settings["retirement_bps"] == 500
    assert settings["require_dual_approval"] is True

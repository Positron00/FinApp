"""Stack-level fault tests (testing.md §8; platform LLD §7.4; HLD §4; C38, C43). They need the
running stack (`scripts/stack.sh up`, profiles core,obs,test) and `scripts/seed.py [--skeleton]
--out ci-logs/seed.json`; without them every test here is skipped. Toxics are always reset, and
every stopped, killed or paused container is started again, in teardown.

Marker `smoke`: the PR fault smoke tests run by scripts/ci/e2e.sh (C38). The rest run nightly in
scripts/ci/nightly-resilience.sh.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from _stack import REPO_ROOT, TIMING_SOURCE, Bff, Containers, Payroll, Stack, Toxiproxy

from scripts.mint_token import load_env


def pytest_report_header() -> str:
    """Says where the payout timings and BREAKER_FAILURES came from (_stack.read_compose_config)."""
    return f"resilience timings: {TIMING_SOURCE}"


@pytest.fixture(scope="session")
def stack() -> Stack:
    env = load_env(REPO_ROOT / ".env")
    seed = REPO_ROOT / "ci-logs" / "seed.json"
    if "TOXIPROXY_PORT" not in env or not seed.is_file():
        pytest.skip("needs the running stack and ci-logs/seed.json (scripts/stack.sh up; seed.py)")
    try:
        httpx.get(f"http://127.0.0.1:{env['TOXIPROXY_PORT']}/version", timeout=2).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("Toxiproxy is not reachable (start the stack with the test profile)")
    users = json.loads(seed.read_text(encoding="utf-8"))["users"]
    return Stack(env=env, owner_id=str(users["owner"]), approver_id=str(users["approver"]))


@pytest.fixture
def toxiproxy(stack: Stack) -> Iterator[Toxiproxy]:
    tp = Toxiproxy(stack.url("TOXIPROXY_PORT"))
    tp.reset()
    try:
        yield tp
    finally:
        tp.reset()
        tp.http.close()


@pytest.fixture
def containers(stack: Stack) -> Iterator[Containers]:
    c = Containers(stack)
    try:
        yield c
    finally:
        c.restore()


@pytest.fixture
def payroll(stack: Stack) -> Iterator[Payroll]:
    p = Payroll(stack)
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def bff(stack: Stack) -> Iterator[Bff]:
    b = Bff(stack)
    try:
        yield b
    finally:
        b.close()

"""Stack-level fault tests (testing.md §8; platform LLD §7.4; C38). They need the running stack
(`scripts/stack.sh up`, profiles core,obs,test) and `scripts/seed.py --skeleton --out
ci-logs/seed.json`; without them every test here is skipped. Toxics are always reset in teardown.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from _stack import REPO_ROOT, Stack, Toxiproxy

from scripts.mint_token import load_env


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
    owner = json.loads(seed.read_text(encoding="utf-8"))["users"]["owner"]
    return Stack(env=env, owner_id=str(owner))


@pytest.fixture
def toxiproxy(stack: Stack) -> Iterator[Toxiproxy]:
    tp = Toxiproxy(stack.url("TOXIPROXY_PORT"))
    tp.reset()
    try:
        yield tp
    finally:
        tp.reset()
        tp.http.close()

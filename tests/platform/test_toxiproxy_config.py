"""deploy/compose/toxiproxy.json matches compose.test.yaml (platform LLD §6.2, §7.4; C46)."""

from __future__ import annotations

import json
import re

from pycommon.testing import find_repo_root

COMPOSE = find_repo_root() / "deploy" / "compose"
EXPECTED = {
    "postgres": ("0.0.0.0:15432", "postgres:5432"),
    "redis": ("0.0.0.0:16379", "redis:6379"),
    "payroll": ("0.0.0.0:18001", "payroll:8000"),
    "bank-stub": ("0.0.0.0:18002", "bank-stub:8000"),
}


def test_exactly_the_four_proxies() -> None:
    proxies = json.loads((COMPOSE / "toxiproxy.json").read_text())
    assert {p["name"]: (p["listen"], p["upstream"]) for p in proxies} == EXPECTED
    assert all(p["enabled"] for p in proxies)


def test_override_uses_the_listen_ports() -> None:
    """Every Toxiproxy port compose.test.yaml points at is a listen port of toxiproxy.json."""
    text = (COMPOSE / "compose.test.yaml").read_text()
    used = set(re.findall(r'(?:_PORT: "|toxiproxy:)(1\d{4})', text))
    assert used == {listen.split(":")[1] for listen, _ in EXPECTED.values()}

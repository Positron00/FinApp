"""Helpers for the stack-level fault tests: the stack's settings and the Toxiproxy API."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.mint_token import mint  # noqa: E402  (repo root added just above)


@dataclass(frozen=True)
class Stack:
    env: dict[str, str]
    owner_id: str

    def url(self, port_key: str) -> str:
        return f"http://127.0.0.1:{self.env[port_key]}"

    def owner_token(self, aud: str) -> str:
        roles = ["admin", "approver", "payroll_admin"]
        return mint(aud, self.owner_id, roles, name="Owner", env=self.env)


class Toxiproxy:
    def __init__(self, base: str) -> None:
        self.http = httpx.Client(base_url=base, timeout=5)

    def add(
        self, proxy: str, name: str, type_: str, stream: str = "downstream", **attrs: int
    ) -> None:
        body = {"name": name, "type": type_, "stream": stream, "attributes": attrs}
        self.http.post(f"/proxies/{proxy}/toxics", json=body).raise_for_status()

    def reset(self) -> None:
        """Re-enable every proxy and remove every toxic (POST /reset)."""
        self.http.post("/reset").raise_for_status()

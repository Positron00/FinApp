"""Idempotency scope and canonical request hash (design §4.8; PAY-400, PAY-405)."""

from __future__ import annotations

from payroll.services.idempotency import request_hash, scope

ROUTE = "/v1/internal/payouts/{payout_id}/result"


def test_hash_is_stable_across_key_order() -> None:
    """PAY-400: the canonical body sorts keys and drops whitespace."""
    a = request_hash("POST", ROUTE, {"payout_id": "p1"}, {"attempt": 1, "outcome": "settled"})
    b = request_hash("post", ROUTE, {"payout_id": "p1"}, {"outcome": "settled", "attempt": 1})
    assert a == b
    assert len(a) == 64


def test_hash_differs_by_param_route_and_body() -> None:
    """PAY-400: path parameter, route and body each change the hash; no body differs from {}."""
    base = request_hash("POST", ROUTE, {"payout_id": "p1"}, {"attempt": 1})
    assert base != request_hash("POST", ROUTE, {"payout_id": "p2"}, {"attempt": 1})
    assert base != request_hash(
        "POST", "/v1/other/{payout_id}", {"payout_id": "p1"}, {"attempt": 1}
    )
    assert base != request_hash("POST", ROUTE, {"payout_id": "p1"}, {"attempt": 2})
    assert request_hash("POST", ROUTE, {}, None) != request_hash("POST", ROUTE, {}, {})


def test_scope_composition() -> None:
    """PAY-400: <iss>|<sub>|<METHOD> <route>|<sorted k=v>, so other users and runs never collide."""
    s = scope("web", "user-1", "post", "/v1/pay-runs/{run_id}/approve", {"run_id": "r1", "a": "z"})
    assert s == "web|user-1|POST /v1/pay-runs/{run_id}/approve|a=z,run_id=r1"
    assert s != scope("web", "user-2", "POST", "/v1/pay-runs/{run_id}/approve", {"run_id": "r1"})

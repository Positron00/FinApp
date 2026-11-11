"""``openapi.json`` matches the app (design §6; PAY-NFR-12)."""

from __future__ import annotations

import json
from pathlib import Path

from bank_stub.main import build_app
from bank_stub.settings import Settings
from pycommon.app import build_openapi

SERVICE_DIR = Path(__file__).resolve().parents[2]


def test_openapi_has_no_drift() -> None:
    """PAY-NFR-12: the committed contract equals the generated one (C59: instrument=False)."""
    spec = build_openapi(lambda s: build_app(s, instrument=False), Settings.model_construct())
    committed = json.loads((SERVICE_DIR / "openapi.json").read_text())
    assert spec == committed


def test_contract_shape() -> None:
    """PAY-412, R6: POST 201/200 and GET 200 bodies are TransferResponse with four fields."""
    spec = json.loads((SERVICE_DIR / "openapi.json").read_text())
    post = spec["paths"]["/v1/transfers"]["post"]
    get = spec["paths"]["/v1/transfers/{key}"]["get"]
    ref = {"$ref": "#/components/schemas/TransferResponse"}
    assert post["responses"]["201"]["content"]["application/json"]["schema"] == ref
    assert post["responses"]["200"]["content"]["application/json"]["schema"] == ref
    assert get["responses"]["200"]["content"]["application/json"]["schema"] == ref
    props = spec["components"]["schemas"]["TransferResponse"]["properties"]
    assert set(props) == {"transfer_id", "status", "reason", "bank_reference"}
    body = post["requestBody"]["content"]["application/json"]["schema"]
    assert set(body["required"]) == {"amount_cents", "currency", "routing", "account"}

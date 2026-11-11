"""Export each service's OpenAPI contract to services/<dir>/openapi.json (platform LLD §3.15).

Usage:
    uv run python scripts/export_openapi.py (<service> ... | --all) [--check]

For identity, payroll and bank_stub (payout-worker has no API) this imports
``<pkg>.main:build_app`` and ``<pkg>.settings:Settings``, builds placeholder settings with
``Settings.model_construct()`` (no env, no I/O) and asks pycommon for the schema. The file is
written with sorted keys and a trailing newline, so the CI drift check
(``git diff --exit-code``) is stable. ``--check`` writes nothing and exits 1 when a file would
change.
"""

from __future__ import annotations

import argparse
import functools
import importlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SERVICES = {"identity": "identity", "payroll": "payroll", "bank_stub": "bank_stub"}


def render(package: str) -> str:
    main = importlib.import_module(f"{package}.main")
    settings_mod = importlib.import_module(f"{package}.settings")
    pycommon_app = importlib.import_module("pycommon.app")
    settings = settings_mod.Settings.model_construct()
    build = main.build_app
    if "instrument" in inspect.signature(build).parameters:
        # Contract export must not start telemetry (platform LLD §3.15: instrument=False).
        build = functools.partial(main.build_app, instrument=False)
    spec: dict[str, Any] = pycommon_app.build_openapi(build, settings)
    return json.dumps(spec, sort_keys=True, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("services", nargs="*", help=f"any of: {', '.join(SERVICES)}")
    parser.add_argument("--all", action="store_true", help="export every service with an API")
    parser.add_argument("--check", action="store_true", help="exit 1 if any file would change")
    args = parser.parse_args(argv)
    targets = list(SERVICES) if args.all else args.services
    if not targets:
        parser.error("name at least one service, or pass --all")
    unknown = sorted(set(targets) - set(SERVICES))
    if unknown:
        parser.error(f"unknown service(s): {', '.join(unknown)}")

    changed: list[str] = []
    for name in targets:
        path = ROOT / "services" / name / "openapi.json"
        text = render(SERVICES[name])
        current = path.read_text() if path.exists() else None
        if current == text:
            continue
        changed.append(str(path.relative_to(ROOT)))
        if not args.check:
            path.write_text(text)

    if args.check and changed:
        print("OpenAPI drift: " + ", ".join(changed), file=sys.stderr)
        return 1
    for item in changed:
        print(f"wrote {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

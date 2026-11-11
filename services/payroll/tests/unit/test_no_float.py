"""AST scan of ``payroll.domain``: no floats, no true division, no Decimal (PAY-NFR-01)."""

from __future__ import annotations

import ast
from pathlib import Path

import payroll.domain
import pytest

DOMAIN = Path(payroll.domain.__file__).parent
FILES = sorted(DOMAIN.rglob("*.py"))


def _violations(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "float"
        ):
            out.append(f"float( at line {node.lineno}")
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            out.append(f"float literal at line {node.lineno}")
        if isinstance(node, ast.BinOp | ast.AugAssign) and isinstance(node.op, ast.Div):
            out.append(f"true division at line {node.lineno}")
        if isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in {"Decimal", "float"}:
                out.append(f"{name} at line {node.lineno}")
        if isinstance(node, ast.ImportFrom) and node.module == "decimal":
            out.append(f"decimal import at line {node.lineno}")
    return out


def test_domain_files_are_scanned() -> None:
    """PAY-NFR-01: the scan covers calc and periods at least."""
    names = {f.name for f in FILES}
    assert {"calc.py", "periods.py", "state.py"} <= names


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_float_in_domain(path: Path) -> None:
    """PAY-NFR-01: no float( call, float literal, `/` division or Decimal in payroll.domain."""
    assert _violations(ast.parse(path.read_text(), filename=str(path))) == []


def test_the_scanner_catches_each_violation() -> None:
    """PAY-NFR-01: the scanner itself flags every banned construct."""
    bad = "from decimal import Decimal\nx = float(1) / 2 + 0.5\ny = Decimal('1')\nz = 3\nz /= 2\n"
    found = _violations(ast.parse(bad))
    assert any("float(" in v for v in found)
    assert any("float literal" in v for v in found)
    assert sum("true division" in v for v in found) == 2
    assert any("Decimal" in v for v in found)
    assert any("decimal import" in v for v in found)

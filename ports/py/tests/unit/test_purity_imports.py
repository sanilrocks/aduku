"""Principle III: the pure phases and schema code must not import storage, audit, time or randomness."""

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "aduku"
FORBIDDEN = (
    "aduku.storage",
    "aduku.audit",
    "aduku.log",
    "aduku.service",
    "sqlite3",
    "time",
    "datetime",
    "random",
    "secrets",
    "uuid",
    "ulid",
    "os",
)


def _imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def _violations(package: str) -> list[str]:
    found: list[str] = []
    for path in sorted((SRC / package).rglob("*.py")):
        for name in _imports(path):
            if any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN):
                found.append(f"{path.relative_to(SRC)} imports {name}")
    return found


def test_engine_is_pure() -> None:
    assert _violations("engine") == []


def test_schema_is_pure() -> None:
    assert _violations("schema") == []


def test_behaviors_are_pure() -> None:
    assert _violations("behaviors") == []

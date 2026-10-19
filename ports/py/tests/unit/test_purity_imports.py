"""Principle III: the pure phases and schema code must not import storage, audit, time or randomness."""

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "aduku"
FORBIDDEN = (
    "aduku.storage",
    "aduku.audit",
    "aduku.log",
    "aduku.service",
    "aduku.hooks",  # the module that calls out: nothing pure may import it
    "aduku.fetch",  # the deferred stage (feature 003): edge modules, never imported by pure code
    "aduku.breaker",
    "aduku.scheduler",
    "sqlite3",
    "time",
    "datetime",
    "random",
    "secrets",
    "uuid",
    "ulid",
    "os",
)
# Feature 003: the planner and the schedule take the time as an argument, so they may name the `datetime` type; they may
# never read a clock (checked below), and every other forbidden import still applies to them.
PURE_MODULES = ("engine/deferred.py", "engine/schedule.py")
CLOCK_CALLS = {"now", "utcnow", "today", "time", "monotonic", "perf_counter", "sleep"}


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
            allowed = str(path.relative_to(SRC)).replace("\\", "/") in PURE_MODULES and name == "datetime"
            if not allowed and any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN):
                found.append(f"{path.relative_to(SRC)} imports {name}")
    return found


def test_engine_is_pure() -> None:
    assert _violations("engine") == []


def test_schema_is_pure() -> None:
    assert _violations("schema") == []


def test_behaviors_are_pure() -> None:
    assert _violations("behaviors") == []


def test_expr_is_pure() -> None:
    """The sandboxed expression language has nothing to reach the outside world with (a package that may not exist yet)."""
    assert _violations("expr") == []


def test_the_deferred_planner_and_schedule_are_pure_modules() -> None:
    """Feature 003 (FR-001): the planner takes `now` as a value and the schedule reads no clock."""
    for name in PURE_MODULES:
        path = SRC / name
        assert path.exists(), f"{name} is missing"
        calls = {
            node.func.attr
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert calls & CLOCK_CALLS == set(), f"{name} reads a clock"

"""The feature 006 example runs and says what the README says it says."""

import importlib.util
import pathlib

EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "examples" / "demo_evolution.py"


def test_the_demo_runs() -> None:
    spec = importlib.util.spec_from_file_location("demo_evolution", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found = module.main()
    assert found["age now"] == 50 and found["overall class"] == "breaking"
    assert found["changes"] == [("merge_changed", "/age", "breaking")]
    assert found["dry run"] == (2, 1, 1, ["acme"]) and found["refused"] == "BREAKING_CHANGE_NOT_ACKNOWLEDGED"
    assert found["age under 2 (acme is pinned)"] == 50 and found["age of the other document"] is None
    assert found["migrated"] == ("acme",) and found["age after the migration"] == 30
    assert found["age after the rollback"] == 50
    assert [a for a, _, _ in found["history"]] == ["bootstrap", "pin", "activate", "migrate", "rollback"]

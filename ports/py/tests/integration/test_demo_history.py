"""The feature 005 example runs and says what the README says it says."""

import importlib.util
import pathlib

EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "examples" / "demo_history.py"


def test_the_demo_runs() -> None:
    spec = importlib.util.spec_from_file_location("demo_history", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found = module.main()
    assert found["now"] == "Annie" and found["as of seq 2"] == "Ann" and found["as of 15 Feb 2024"] == "Anna"
    assert found["entries after compaction"] == 1 and found["as of seq 2 (archive kept)"] == "Ann"
    assert found["email after purge"] is None and found["purge log"] == [("ticket 4711", "officer")]
    assert found["rebuild identical"] is True

"""Feature 006 (contracts/cli.md): schema diff, plan, activate and history on the command line."""

import pathlib

import pytest

from tests.integration.test_cli import run
from tests.schema_helpers import field_text, versions_text

BASE = field_text()
BREAKING = BASE.replace('"/age": {type: int, merge: max}', '"/age": {type: int, merge: min}')
VISIBLE = BASE.replace('"/age": {type: int, merge: max}', '"/age": {type: int, merge: max, default: 0}')
COMPATIBLE = BASE + ', "/nick": {type: string, merge: lww}'


@pytest.fixture
def files(tmp_path: pathlib.Path) -> pathlib.Path:
    folder = tmp_path / "schemas"
    folder.mkdir()
    for number, text in enumerate(versions_text(BASE, BREAKING, VISIBLE, COMPATIBLE), start=1):
        (folder / f"v{number}.ion").write_text(text)
    return folder


@pytest.fixture
def env(tmp_path: pathlib.Path, files: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> list[str]:
    """A database holding two documents written under version 1, which is the active one."""
    db = str(tmp_path / "cli.db")
    first = ["--db", db, "--schema", str(files / "v1.ion")]
    for doc, sets in (("d1", ('/name="A"', "/age=3", "/age=5")), ("d2", ('/name="B"',))):
        args = ["append", doc, "--contributor", "alice", "--tier", "user"]
        for item in sets:
            args += ["--set", item]
        assert run(capsys, *first, *args)[0] == 0
    return ["--db", db, "--schema", str(files)]


def test_diff_prints_the_change_list_and_the_overall_class_and_needs_no_store(
    files: pathlib.Path, capsys: pytest.CaptureFixture[str], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, _ = run(capsys, "schema", "diff", str(files / "v1.ion"), str(files / "v2.ion"))
    assert code == 0 and (out["old"], out["new"], out["overall"]) == (1, 2, "breaking")
    assert [(c["kind"], c["path"], c["class"], c["classified"]) for c in out["changes"]] == [
        ("merge_changed", "/age", "breaking", True)
    ]
    assert out["changes"][0]["description"]
    assert not (tmp_path / "aduku.db").exists()  # no store was opened
    code, out, _ = run(capsys, "schema", "diff", str(files / "v1.ion"), str(files / "v1.ion"))
    assert code == 0 and out["overall"] == "compatible" and out["changes"] == []


def test_diff_of_a_file_that_is_missing_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], tmp_path: pathlib.Path
) -> None:
    code, _, err = run(capsys, "schema", "diff", str(tmp_path / "nope.ion"), str(tmp_path / "nope2.ion"))
    assert code == 2 and "cannot read schema" in err


def test_plan_prints_counts_and_what_differs_without_values(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, *env, "schema", "plan", "--target", "2")
    assert code == 0
    assert {k: out[k] for k in ("target", "examined", "left_out", "changed", "unchanged", "failed", "would_call_hook")} == {
        "target": 2, "examined": 2, "left_out": 0, "changed": 1, "unchanged": 1, "failed": 0, "would_call_hook": 0,
    }  # fmt: skip
    (changed,) = out["documents"]
    assert changed["doc"] == "d1" and changed["document_changed"] is True
    assert set(changed) >= {"errors_added", "errors_resolved", "skipped_before", "skipped_after", "shadowed_by_reason"}
    assert "generation" in out and out["changes"][0]["kind"] == "merge_changed"
    assert '"A"' not in str(out) and "name" not in str(changed)


def test_plan_takes_a_limit_and_a_list_of_documents(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, *env, "schema", "plan", "--target", "2", "--doc", "d2")
    assert (code, out["examined"], out["left_out"]) == (0, 1, 1)
    code, out, _ = run(capsys, *env, "schema", "plan", "--target", "2", "--limit", "1")
    assert (code, out["examined"], out["left_out"]) == (0, 1, 1)


def test_plan_of_a_version_the_files_lack_is_a_refusal(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, *env, "schema", "plan", "--target", "9")
    assert code == 1 and out["rejected"]["code"] == "SCHEMA_INACTIVE"


def test_activate_a_breaking_version_needs_the_acknowledgement(
    env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, *env, "schema", "activate", "2", "--operator", "alice")
    assert code == 1 and out["rejected"]["code"] == "BREAKING_CHANGE_NOT_ACKNOWLEDGED"
    assert "merge_changed" in err and "/age" in err
    code, out, _ = run(capsys, *env, "schema", "activate", "2", "--operator", "alice", "--acknowledge-breaking")
    assert code == 0 and (out["version"], out["previous"], out["class"], out["rollback"]) == (2, 1, "breaking", False)
    code, out, _ = run(capsys, *env, "get", "d1")
    assert out["document"] == '{age:3,name:"A"}'


def test_activate_with_rebuild_prints_what_was_refreshed_and_what_changed(
    env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(
        capsys, *env, "schema", "activate", "2", "--operator", "alice", "--acknowledge-breaking", "--rebuild"
    )
    assert code == 0 and out["changed"] == ["d1"]
    assert sorted(out["rebuild"]["checked"]) == ["d1", "d2"] and out["rebuild"]["failed"] == []


def test_activate_a_compatible_version_and_then_roll_back(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, *env, "schema", "activate", "4", "--operator", "alice")
    assert code == 0 and out["class"] == "compatible"
    code, out, _ = run(capsys, *env, "schema", "activate", "1", "--operator", "alice")
    assert code == 0 and out["rollback"] is True


def test_activate_without_an_operator_is_a_usage_error(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, *env, "schema", "activate", "4")
    assert code == 2 and "operator" in err
    code, _, _ = run(capsys, *env, "schema", "activate", "4", "--operator", "  ")
    assert code == 2


def test_history_lists_the_acts_and_can_be_limited_and_filtered(
    env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, *env, "schema", "activate", "4", "--operator", "alice")
    run(capsys, *env, "schema", "activate", "1", "--operator", "bob")
    code, out, _ = run(capsys, *env, "schema", "history")
    assert code == 0
    assert [(r["action"], r["operator"], r["from"], r["to"]) for r in out["history"]] == [
        ("bootstrap", "bootstrap", None, 1),
        ("activate", "alice", 1, 4),
        ("rollback", "bob", 4, 1),
    ]
    assert set(out["history"][1]) >= {"id", "at", "operator", "action", "doc", "from", "to", "class", "note"}
    code, out, _ = run(capsys, *env, "schema", "history", "--limit", "1")
    assert [r["operator"] for r in out["history"]] == ["bob"]


def test_a_directory_of_versions_and_a_repeated_schema_option_serve_the_same_registry(
    env: list[str], files: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repeated = [env[0], env[1], "--schema", str(files / "v1.ion"), "--schema", str(files / "v2.ion")]
    code, out, _ = run(capsys, *repeated, "schema", "plan", "--target", "2")
    assert code == 0 and out["changed"] == 1


def test_history_prints_the_fingerprint_of_each_served_version(
    env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, *env, "schema", "activate", "4", "--operator", "alice")
    code, out, _ = run(capsys, *env, "schema", "history")
    assert code == 0 and sorted(out["served"]) == ["1", "4"]
    assert all(len(fp) == 64 for fp in out["served"].values())

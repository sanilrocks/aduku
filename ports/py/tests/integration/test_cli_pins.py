"""Feature 006 (contracts/cli.md): pin, unpin and migrate on the command line."""

import pathlib

import pytest

from tests.integration.test_cli import run
from tests.schema_helpers import field_text, versions_text

BASE = field_text()
BREAKING = BASE.replace('"/age": {type: int, merge: max}', '"/age": {type: int, merge: min}')
THIRD = BASE + ', "/nick": {type: string, merge: lww}'


@pytest.fixture
def env(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> list[str]:
    folder = tmp_path / "schemas"
    folder.mkdir()
    for number, text in enumerate(versions_text(BASE, BREAKING, THIRD), start=1):
        (folder / f"v{number}.ion").write_text(text)
    db = str(tmp_path / "cli.db")
    first = ["--db", db, "--schema", str(folder / "v1.ion")]
    for doc in ("d1", "d2", "d3"):
        code, _, _ = run(
            capsys, *first, "append", doc, "--contributor", "alice", "--tier", "user", "--set", '/name="A"',
            "--set", "/age=3", "--set", "/age=5",
        )  # fmt: skip
        assert code == 0
    return ["--db", db, "--schema", str(folder)]


def test_pin_and_unpin_move_a_document_between_its_version_and_the_active_one(
    env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, *env, "pin", "d1", "1", "--operator", "alice")[0] == 0
    assert run(capsys, *env, "schema", "activate", "2", "--operator", "alice", "--acknowledge-breaking")[0] == 0
    assert run(capsys, *env, "get", "d1")[1]["document"] == '{age:5,name:"A"}'  # held on version 1
    assert run(capsys, *env, "get", "d2")[1]["document"] == '{age:3,name:"A"}'
    code, out, _ = run(capsys, *env, "unpin", "d1", "--operator", "bob")
    assert code == 0 and out["unpinned"] == "d1"
    assert run(capsys, *env, "get", "d1")[1]["document"] == '{age:3,name:"A"}'
    history = run(capsys, *env, "schema", "history", "--doc", "d1")[1]["history"]
    assert [(r["action"], r["operator"]) for r in history] == [("pin", "alice"), ("unpin", "bob")]


def test_pin_to_a_version_the_files_lack_is_a_refusal_with_its_code(
    env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, *env, "pin", "d1", "9", "--operator", "alice")
    assert code == 1 and out["rejected"]["code"] == "SCHEMA_INACTIVE"


def test_pin_without_an_operator_is_a_usage_error(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (("pin", "d1", "1"), ("unpin", "d1"), ("migrate", "d1")):
        code, _, err = run(capsys, *env, *argv)
        assert code == 2 and "operator" in err


def prepare(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, *env, "pin", "d1", "1", "--operator", "alice")
    run(capsys, *env, "pin", "d2", "1", "--operator", "alice")
    run(capsys, *env, "schema", "activate", "2", "--operator", "alice", "--acknowledge-breaking")


def test_migrate_moves_pinned_documents_to_the_active_version(
    env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    prepare(env, capsys)
    code, out, _ = run(capsys, *env, "migrate", "d1", "d3", "--operator", "alice")
    assert code == 0 and out["moved"] == ["d1"] and out["not_pinned"] == ["d3"] and out["failed"] == []
    assert out["already_moved"] == [] and out["batches"] == 1
    assert run(capsys, *env, "get", "d1")[1]["document"] == '{age:3,name:"A"}'
    code, out, _ = run(capsys, *env, "migrate", "--operator", "alice", "--batch", "1")
    assert code == 0 and out["moved"] == ["d2"] and out["batches"] == 1


def test_migrate_to_a_chosen_version_keeps_the_pin_there(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    prepare(env, capsys)
    code, out, _ = run(capsys, *env, "migrate", "d1", "--to", "3", "--operator", "alice")
    assert code == 0 and out["moved"] == ["d1"]
    history = run(capsys, *env, "schema", "history", "--doc", "d1")[1]["history"]
    assert (history[-1]["action"], history[-1]["from"], history[-1]["to"]) == ("migrate", 1, 3)


def test_migrate_dry_run_prints_what_would_change_and_moves_nothing(
    env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    prepare(env, capsys)
    code, out, _ = run(capsys, *env, "migrate", "--dry-run", "--operator", "alice")
    assert code == 0 and out["target"] == 2
    assert [(d["doc"], d["document_changed"]) for d in out["documents"]] == [("d1", True), ("d2", True)]
    assert run(capsys, *env, "get", "d1")[1]["document"] == '{age:5,name:"A"}'  # still pinned


def test_migrate_of_one_document_that_is_not_pinned_is_a_refusal(
    env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    prepare(env, capsys)
    code, out, _ = run(capsys, *env, "migrate", "d3", "--operator", "alice")
    assert code == 1 and out["rejected"]["code"] == "NOT_PINNED"
    code, out, _ = run(capsys, *env, "migrate", "d3", "--dry-run", "--operator", "alice")
    assert code == 1 and out["rejected"]["code"] == "NOT_PINNED"


def test_migrate_to_a_version_the_files_lack_is_a_refusal(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    prepare(env, capsys)
    code, out, _ = run(capsys, *env, "migrate", "d1", "--to", "9", "--operator", "alice")
    assert code == 1 and out["rejected"]["code"] == "SCHEMA_INACTIVE"

"""Feature 006 (contracts/cli.md): `get` prints `shadowed` beside `skipped`, `explain` prints the shadowed entries of the path."""

import pathlib

import pytest

from aduku.storage.sqlite import SqliteStorage
from tests.integration.test_cli import run
from tests.schema_helpers import Env, field_text, versions_text

V1 = field_text(', "/nick": {type: string, merge: lww}')
V2 = field_text(', "/nick": {type: string, computed: {macro: upper, args: ["/name"]}}').replace(
    '"/age": {type: int, merge: max}', '"/age": {type: string, merge: lww}'
)


@pytest.fixture
def env(tmp_path: pathlib.Path) -> list[str]:
    folder = tmp_path / "schemas"
    folder.mkdir()
    for number, text in enumerate(versions_text(V1, V2), start=1):
        (folder / f"v{number}.ion").write_text(text)
    return ["--db", str(tmp_path / "cli.db"), "--schema", str(folder)]


def seed(capsys: pytest.CaptureFixture[str], env: list[str], tmp_path: pathlib.Path) -> None:
    """Write under version 1 through the command line, then activate version 2 as another process would."""
    code, _, _ = run(
        capsys, *env[:2], "--schema", str(tmp_path / "schemas" / "v1.ion"), "append", "d1", "--contributor", "alice",
        "--tier", "user", "--set", '/name="A"', "--set", "/age=3", "--set", '/nick="n"',
    )  # fmt: skip
    assert code == 0
    storage = SqliteStorage(env[1])
    try:
        Env(storage, *versions_text(V1, V2)).service(active=1).activate_schema(
            2, operator="alice", acknowledge_breaking=True
        )
    finally:
        storage.close()


def test_get_prints_shadowed_beside_skipped(
    env: list[str], capsys: pytest.CaptureFixture[str], tmp_path: pathlib.Path
) -> None:
    seed(capsys, env, tmp_path)
    code, out, _ = run(capsys, *env, "get", "d1")
    assert code == 0 and out["skipped"] == [2, 3]
    assert [(s["seq"], s["tier"], s["path"], s["reason"]) for s in out["shadowed"]] == [
        (2, "user", "/age", "no_longer_conforms"),
        (3, "user", "/nick", "now_derived"),
    ]
    assert all(set(s) == {"seq", "entry", "tier", "path", "reason", "detail"} for s in out["shadowed"])


def test_get_under_the_version_that_wrote_the_entries_prints_an_empty_list(
    env: list[str], capsys: pytest.CaptureFixture[str], tmp_path: pathlib.Path
) -> None:
    code, _, _ = run(
        capsys, *env[:2], "--schema", str(tmp_path / "schemas" / "v1.ion"), "append", "d1", "--contributor", "alice",
        "--tier", "user", "--set", '/name="A"',
    )  # fmt: skip
    assert code == 0
    code, out, _ = run(capsys, *env, "get", "d1")
    assert code == 0 and out["skipped"] == [] and out["shadowed"] == []


def test_explain_prints_the_shadowed_entries_that_touch_the_path(
    env: list[str], capsys: pytest.CaptureFixture[str], tmp_path: pathlib.Path
) -> None:
    seed(capsys, env, tmp_path)
    code, out, _ = run(capsys, *env, "explain", "d1", "/nick")
    assert code == 0 and [(s["seq"], s["reason"]) for s in out["shadowed_by_schema"]] == [(3, "now_derived")]
    assert isinstance(out["shadowed"], list)  # the Feature 001 field is still the ids that make no difference
    code, out, _ = run(capsys, *env, "explain", "d1", "/name")
    assert code == 0 and out["shadowed_by_schema"] == []


def test_a_schema_directory_loads_every_ion_file_as_a_version_and_a_file_may_be_repeated(
    env: list[str], capsys: pytest.CaptureFixture[str], tmp_path: pathlib.Path
) -> None:
    both = [
        "--db",
        env[1],
        "--schema",
        str(tmp_path / "schemas" / "v1.ion"),
        "--schema",
        str(tmp_path / "schemas" / "v2.ion"),
    ]
    code, _, _ = run(capsys, *both, "entries", "d1")
    assert code == 0


def test_a_directory_without_schema_files_is_a_usage_error(
    env: list[str], capsys: pytest.CaptureFixture[str], tmp_path: pathlib.Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    code, _, err = run(capsys, "--db", env[1], "--schema", str(empty), "entries", "d1")
    assert code == 2 and "no schema" in err

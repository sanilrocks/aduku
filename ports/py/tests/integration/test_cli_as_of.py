"""Feature 005: `get --as-of`, `explain --as-of`, `append --valid-time`, and `entries` showing the new fields."""

import pathlib

import pytest

from tests.integration.test_cli import SCHEMA, run


@pytest.fixture
def env(tmp_path: pathlib.Path) -> list[str]:
    schema = tmp_path / "t.schema.ion"
    schema.write_text(SCHEMA)
    return ["--db", str(tmp_path / "cli.db"), "--schema", str(schema)]


def seed(capsys: pytest.CaptureFixture[str], env: list[str]) -> list[str]:
    for name in ("one", "two", "three"):
        run(capsys, *env, "append", "d1", "--contributor", "alice", "--tier", "user", "--set", f'/name="{name}"')
    code, out, _ = run(capsys, *env, "entries", "d1")
    return [e["tx_time"] for e in out["entries"]]


def test_get_as_of_seq_prints_the_old_document_and_where_it_stands(
    env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    seed(capsys, env)
    code, out, _ = run(capsys, *env, "get", "d1", "--as-of", "seq=2")
    assert code == 0 and out["document"] == '{name:"two"}'
    assert out["as_of"] == {"kind": "seq", "point": "2", "resolved_seq": 2, "included": 2, "inside_request": False}
    code, out, _ = run(capsys, *env, "get", "d1")
    assert out["document"] == '{name:"three"}' and out.get("as_of") is None


def test_get_as_of_tx_and_valid_and_explain_as_of(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    times = seed(capsys, env)
    code, out, _ = run(capsys, *env, "get", "d1", "--as-of", f"tx={times[1]}")
    assert code == 0 and out["document"] == '{name:"two"}' and out["as_of"]["kind"] == "tx_time"
    run(
        capsys,
        *env,
        "append",
        "d1",
        "--contributor",
        "alice",
        "--tier",
        "user",
        "--set",
        "/age=5",
        "--valid-time",
        "2024-03-01T00:00:00Z",
    )
    code, out, _ = run(capsys, *env, "get", "d1", "--as-of", "valid=2024-01-01T00:00:00Z")
    assert code == 0 and "age" not in out["document"] and out["as_of"]["included"] == 3
    code, out, _ = run(capsys, *env, "explain", "d1", "/name", "--as-of", "seq=1")
    assert code == 0 and out["value"] == '"one"' and out["winner_seq"] == 1


@pytest.mark.parametrize(
    "args",
    [
        ["--as-of", "seq=1", "--as-of", "valid=2024-01-01T00:00:00Z"],
        ["--as-of", "seq=1", "--as-of", "seq=2"],
        ["--as-of", "bogus"],
        ["--as-of", "seq=x"],
    ],
)
def test_several_or_malformed_points_are_a_usage_error(
    env: list[str], capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    seed(capsys, env)
    code, _, err = run(capsys, *env, "get", "d1", *args)
    assert code == 2 and ("AS_OF_AMBIGUOUS" in err or "as-of" in err)


def test_entries_show_valid_time_and_a_bad_one_is_rejected(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    run(
        capsys,
        *env,
        "append",
        "d1",
        "--contributor",
        "alice",
        "--tier",
        "user",
        "--set",
        '/name="x"',
        "--valid-time",
        "2024-03-01T12:00:00+02:00",
    )
    code, out, _ = run(capsys, *env, "entries", "d1")
    assert out["entries"][0]["valid_time"] == "2024-03-01T10:00:00+00:00"
    code, out, err = run(
        capsys,
        *env,
        "append",
        "d1",
        "--contributor",
        "alice",
        "--tier",
        "user",
        "--set",
        '/name="y"',
        "--valid-time",
        "2024-03-01T12:00:00",
    )
    assert code == 1 and out["rejected"]["code"] == "BAD_VALID_TIME"
    code, _, err = run(
        capsys,
        *env,
        "append",
        "d1",
        "--contributor",
        "alice",
        "--tier",
        "user",
        "--set",
        '/name="y"',
        "--valid-time",
        "not a time",
    )
    assert code == 2

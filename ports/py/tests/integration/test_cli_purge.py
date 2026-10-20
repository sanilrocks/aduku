"""Feature 005: `purge` and `purge-log` (the caller options are those of `recompute`; --scope-tier narrows the scope)."""

import pathlib

import pytest

from tests.integration.test_cli import run

SCHEMA = """schema::{id: "t", version: 1, tiers: [backfill, user, admin], roles: [dpo], purge_by: [dpo],
  fields: {"/name": {type: string, merge: lww}, "/age": {type: int, merge: max}}}"""
CALLER = ["--contributor", "officer", "--tier", "admin", "--role", "dpo"]


@pytest.fixture
def env(tmp_path: pathlib.Path) -> list[str]:
    schema = tmp_path / "t.schema.ion"
    schema.write_text(SCHEMA)
    return ["--db", str(tmp_path / "cli.db"), "--schema", str(schema)]


def seed(capsys: pytest.CaptureFixture[str], env: list[str]) -> None:
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
        '/name="secret-marker"',
        "--set",
        "/age=3",
    )
    run(capsys, *env, "append", "d1", "--contributor", "alice", "--tier", "user", "--set", '/name="later"')


def test_purge_prints_counts_and_never_a_value_and_the_log_lists_it(
    env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    seed(capsys, env)
    code, out, err = run(capsys, *env, "purge", "d1", "--path", "/name", "--reason", "ticket 4711", *CALLER)
    assert code == 0 and out["entries_erased"] == 2 and out["purge"] and "secret-marker" not in str(out) + err
    code, out, _ = run(capsys, *env, "purge-log", "d1")
    assert code == 0 and [(r["reason"], r["actor"], r["scope"]) for r in out["purges"]] == [
        ("ticket 4711", "officer", '{path:"/name"}')
    ]
    assert "secret-marker" not in str(out)
    code, out, _ = run(capsys, *env, "entries", "d1")
    assert [e.get("purged") for e in out["entries"]] == [True, None, True] and all(
        "secret-marker" not in str(e) for e in out["entries"]
    )
    code, out, _ = run(capsys, *env, "get", "d1")
    assert out["document"] == "{age:3}"


def test_purge_by_entry_and_by_scope_tier(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    seed(capsys, env)
    code, entries, _ = run(capsys, *env, "entries", "d1")
    first = entries["entries"][0]["id"]
    code, out, _ = run(capsys, *env, "purge", "d1", "--entry", first, "--reason", "r", *CALLER)
    assert code == 0 and out["entries_erased"] == 1
    code, out, _ = run(
        capsys, *env, "purge", "d1", "--path", "/name", "--scope-tier", "admin", "--reason", "r", *CALLER
    )
    assert code == 0 and out["entries_erased"] == 0


def test_refusals_and_usage_errors(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    seed(capsys, env)
    code, out, _ = run(
        capsys, *env, "purge", "d1", "--path", "/name", "--reason", "r", "--contributor", "intern", "--tier", "user"
    )
    assert code == 1 and out["rejected"]["code"] == "NOT_AUTHORIZED"
    code, _, err = run(capsys, *env, "purge", "d1", "--path", "/name", *CALLER)
    assert code == 2  # --reason is required
    code, _, err = run(capsys, *env, "purge", "d1", "--reason", "r", *CALLER)
    assert code == 2 and "--path" in err
    code, _, err = run(capsys, *env, "purge", "d1", "--path", "/name", "--entry", "x", "--reason", "r", *CALLER)
    assert code == 2
    code, out, _ = run(capsys, *env, "purge", "d1", "--path", "/name", "--reason", " ", *CALLER)
    assert code == 1 and out["rejected"]["code"] == "PURGE_REASON_REQUIRED"


def test_explain_prints_the_purge_marks(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    seed(capsys, env)
    code, entries, _ = run(capsys, *env, "entries", "d1")
    first = entries["entries"][0]
    run(capsys, *env, "purge", "d1", "--entry", first["id"], "--reason", "ticket 9", *CALLER)
    code, out, _ = run(capsys, *env, "explain", "d1", "/name")
    assert code == 0 and out["originals_kept"] is None and out["sources"] == []
    assert [(m["entry"], m["seq"], m["reason"]) for m in out["purged"]] == [(first["id"], 1, "ticket 9")]
    assert out["purged"][0]["at"] and first["id"] not in [out["winner"], *out["inputs"], *out["shadowed"]]
    code, out, _ = run(capsys, *env, "explain", "d1", "/age")
    assert out["purged"] == []

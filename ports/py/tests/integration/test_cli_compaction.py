"""Feature 005: `compact`, `archive verify`, `archive expire` and `entries` showing synthetic entries."""

import pathlib

import pytest

from tests.integration.test_cli import SCHEMA, run


@pytest.fixture
def env(tmp_path: pathlib.Path) -> list[str]:
    schema = tmp_path / "t.schema.ion"
    schema.write_text(SCHEMA)
    return ["--db", str(tmp_path / "cli.db"), "--schema", str(schema)]


def seed(capsys: pytest.CaptureFixture[str], env: list[str]) -> None:
    for i in range(4):
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
            f'/name="n{i}"',
            "--set",
            f"/age={i}",
        )


def test_compact_reports_what_it_did_and_entries_show_the_synthetic_one(
    env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    seed(capsys, env)
    code, out, _ = run(capsys, *env, "compact", "d1", "--through-seq", "6")
    assert (
        code == 0 and out["noop"] is False and out["watermark"] == 6 and out["replaced"] == 6 and out["synthetic"] == 1
    )
    assert out["checked_paths"] == 2 and out["compaction"]
    code, out, _ = run(capsys, *env, "entries", "d1")
    assert [(e["seq"], e["op"]) for e in out["entries"]] == [(6, "layer"), (7, "set"), (8, "set")]
    assert out["entries"][0]["synthetic"] and "purged" not in out["entries"][0]
    code, out, _ = run(capsys, *env, "get", "d1")
    assert out["document"] == '{age:3,name:"n3"}'


def test_nothing_to_compact_is_not_an_error(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    seed(capsys, env)
    code, out, _ = run(capsys, *env, "compact", "d1", "--through-seq", "0")
    assert code == 0 and out == {"noop": True, "message": "nothing to compact"}


def test_compact_by_age_and_usage_errors(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    seed(capsys, env)
    code, out, _ = run(capsys, *env, "compact", "d1", "--older-than", "1d")
    assert code == 0 and out["noop"] is True  # nothing is a day old
    code, _, err = run(capsys, *env, "compact", "d1", "--older-than", "soon")
    assert code == 2
    code, _, err = run(capsys, *env, "compact", "d1")
    assert code == 2


def test_archive_verify_and_expire(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    seed(capsys, env)
    run(capsys, *env, "compact", "d1", "--through-seq", "6")
    code, out, _ = run(capsys, *env, "archive", "verify", "d1")
    assert code == 0 and out == {"doc": "d1", "checked": True, "differences": []}
    code, out, _ = run(capsys, *env, "archive", "expire", "d1", "--through-seq", "6")
    assert code == 0 and out == {"doc": "d1", "through_seq": 6, "expired": 6}
    code, out, _ = run(capsys, *env, "archive", "verify", "d1")
    assert code == 0 and out["checked"] is False
    code, out, err = run(capsys, *env, "get", "d1", "--as-of", "seq=3")
    assert code == 1 and out["rejected"]["code"] == "HISTORY_COMPACTED"


def test_explain_says_whether_the_originals_are_kept_and_lists_the_sources(
    env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    seed(capsys, env)
    run(capsys, *env, "compact", "d1", "--through-seq", "6")
    code, out, _ = run(capsys, *env, "explain", "d1", "/name")
    assert code == 0 and out["originals_kept"] == "kept" and out["sources"] == [] and out["purged"] == []
    run(capsys, *env, "archive", "expire", "d1", "--through-seq", "6")
    code, out, _ = run(capsys, *env, "explain", "d1", "/age")
    assert out["originals_kept"] == "expired" and len(out["sources"]) >= 1
    code, out, _ = run(capsys, *env, "entries", "d1")
    assert out["entries"][0]["synthetic"]

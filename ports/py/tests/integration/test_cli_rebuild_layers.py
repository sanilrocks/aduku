"""Feature 005: `rebuild` prints the layers that differed or were refreshed (contracts/cli.md)."""

import pathlib
import sqlite3

import pytest

from tests.integration.test_cli import SCHEMA, run


@pytest.fixture
def env(tmp_path: pathlib.Path) -> list[str]:
    schema = tmp_path / "t.schema.ion"
    schema.write_text(SCHEMA)
    return ["--db", str(tmp_path / "cli.db"), "--schema", str(schema)]


def test_rebuild_lists_layers_refreshed_then_differing(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, *env, "append", "d1", "--contributor", "alice", "--tier", "user", "--set", '/name="A"')
    db = pathlib.Path(env[1])
    connection = sqlite3.connect(db)
    connection.execute('UPDATE layers SET state = \'[{p:"/name",s:"tampered"}]\'')
    connection.commit()
    code, out, _ = run(capsys, *env, "rebuild", "d1")
    assert code == 1 and out["identical"] is False
    assert out["layers_differing"] == [{"doc": "d1", "tier": "user"}] and out["layers_refreshed"] == []
    connection.execute("DELETE FROM layers")
    connection.commit()
    code, out, _ = run(capsys, *env, "rebuild", "d1")
    assert code == 0 and out["layers_refreshed"] == [{"doc": "d1", "tier": "user"}] and out["layers_differing"] == []
    code, out, _ = run(capsys, *env, "rebuild", "d1")
    assert code == 0 and out["layers_refreshed"] == [] and out["layers_differing"] == []
    connection.close()

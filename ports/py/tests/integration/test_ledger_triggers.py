"""Feature 005: entries still cannot be updated or deleted by ordinary statements; only a rewriting operation may."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aduku.model import Entry
from aduku.storage.base import RequestRecord
from aduku.storage.sqlite import SqliteStorage


@pytest.fixture
def path(tmp_path: Path) -> str:
    db = str(tmp_path / "t.db")
    storage = SqliteStorage(db)
    entry = Entry("e1", "d", 1, "r", "user", "/a", "set", 1, "c", datetime(2026, 1, 1, tzinfo=UTC), 1)
    with storage.transaction() as tx:
        tx.append_request(RequestRecord("r", "d", "c"), [entry])
    storage.close()
    return db


def test_plain_update_and_delete_still_fail(path: str) -> None:
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        connection.execute("UPDATE entries SET path = '/b'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        connection.execute("DELETE FROM entries")
    connection.close()


def test_the_flag_lifts_the_triggers_and_is_cleared_with_the_transaction(path: str) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("UPDATE ledger_rewrite SET active = 1 WHERE id = 1")
    connection.execute("UPDATE entries SET path = '/b'")
    connection.execute("ROLLBACK")  # a failed rewrite: the flag goes back with everything else
    assert connection.execute("SELECT active FROM ledger_rewrite").fetchone() == (0,)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        connection.execute("UPDATE entries SET path = '/c'")
    assert connection.execute("SELECT path FROM entries").fetchone() == ("/a",)
    connection.close()

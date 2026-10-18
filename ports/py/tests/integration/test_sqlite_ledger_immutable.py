"""Principle I: the ledger table is append-only and the constraints from data-model.md hold at the database level."""

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from aduku.storage.sqlite import SqliteStorage

ENTRY = {
    "id": "e1",
    "doc": "d1",
    "seq": 1,
    "request_id": "r1",
    "tier": "user",
    "path": "/name",
    "op": "set",
    "value": '"Alice"',
    "contributor": "alice",
    "tx_time": "2026-10-18T12:00:00+00:00",
    "idem_key": None,
    "request_key": None,
    "request_hash": None,
    "schema_version": 1,
    "lenient": 0,
    "meta": None,
}


@pytest.fixture
def raw(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    path = str(tmp_path / "ledger.db")
    storage = SqliteStorage(path)
    connection = sqlite3.connect(path, isolation_level=None)
    yield connection
    connection.close()
    storage.close()


def insert(connection: sqlite3.Connection, **overrides: object) -> None:
    row = {**ENTRY, **overrides}
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    connection.execute(f"INSERT INTO entries ({columns}) VALUES ({marks})", list(row.values()))


def test_uses_wal_and_expected_tables(raw: sqlite3.Connection) -> None:
    assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"entries", "doc_seq", "requests", "views", "rejection_audit"} <= tables


def test_entries_cannot_be_updated(raw: sqlite3.Connection) -> None:
    insert(raw)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("UPDATE entries SET value = '\"Mallory\"' WHERE id = 'e1'")


def test_entries_cannot_be_deleted(raw: sqlite3.Connection) -> None:
    insert(raw)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("DELETE FROM entries WHERE id = 'e1'")


def test_doc_seq_pair_is_unique(raw: sqlite3.Connection) -> None:
    insert(raw)
    with pytest.raises(sqlite3.IntegrityError):
        insert(raw, id="e2")


def test_idem_key_is_unique_per_document_when_present(raw: sqlite3.Connection) -> None:
    insert(raw, idem_key="k")
    with pytest.raises(sqlite3.IntegrityError):
        insert(raw, id="e2", seq=2, idem_key="k")
    insert(raw, id="e3", seq=3, doc="d2", idem_key="k")
    insert(raw, id="e4", seq=2, idem_key=None)
    insert(raw, id="e5", seq=3, idem_key=None)


def test_request_key_is_unique_per_request(raw: sqlite3.Connection) -> None:
    raw.execute(
        "INSERT INTO requests (request_id, doc, contributor, request_key, request_hash) VALUES ('r1','d1','alice','K','h')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO requests (request_id, doc, contributor, request_key, request_hash) VALUES ('r2','d1','alice','K','h')"
        )
    raw.execute(
        "INSERT INTO requests (request_id, doc, contributor, request_key, request_hash) VALUES ('r3','d1','bob','K','h')"
    )
    raw.execute(
        "INSERT INTO requests (request_id, doc, contributor, request_key, request_hash) VALUES ('r4','d1','alice',NULL,NULL)"
    )
    raw.execute(
        "INSERT INTO requests (request_id, doc, contributor, request_key, request_hash) VALUES ('r5','d1','alice',NULL,NULL)"
    )

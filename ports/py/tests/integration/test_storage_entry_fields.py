"""Feature 005: valid_time, synthetic and purged round-trip through both backends; an older database opens unchanged."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from aduku.model import Entry
from aduku.storage.base import RequestRecord, Storage
from aduku.storage.sqlite import SqliteStorage

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def entry(seq: int, **kwargs: object) -> Entry:
    base = dict(
        id=f"e{seq}",
        doc="d",
        seq=seq,
        request_id="r1",
        tier="user",
        path="/a",
        op="set",
        value=seq,
        contributor="c",
        tx_time=T0,
        schema_version=1,
    )
    base.update(kwargs)
    return Entry(**base)  # type: ignore[arg-type]


def test_valid_time_round_trips(backend: Storage) -> None:
    moment = datetime(2024, 3, 1, 12, 30, tzinfo=UTC)
    with backend.transaction() as tx:
        tx.append_request(RequestRecord("r1", "d", "c"), [entry(1, valid_time=moment), entry(2)])
    with backend.read() as tx:
        rows = tx.entries("d")
    assert rows[0].valid_time == moment
    assert rows[1].valid_time is None
    assert rows[0].synthetic is None and rows[0].purged is False


def test_a_database_made_before_this_feature_opens_and_reads_unchanged(tmp_path: Path) -> None:
    path = str(tmp_path / "old.db")
    SqliteStorage(path).close()
    connection = sqlite3.connect(path)
    # make it look like a Feature 4 database: no new columns, the old unconditional triggers
    connection.executescript(
        """
        DROP TRIGGER entries_no_update; DROP TRIGGER entries_no_delete;
        ALTER TABLE entries DROP COLUMN valid_time; ALTER TABLE entries DROP COLUMN synthetic;
        ALTER TABLE entries DROP COLUMN purged; ALTER TABLE views DROP COLUMN epoch;
        INSERT INTO entries (id, doc, seq, request_id, tier, path, op, value, contributor, tx_time, schema_version)
          VALUES ('x', 'd', 1, 'r', 'user', '/a', 'set', '1', 'c', '2026-01-01T00:00:00.000000Z', 1);
        INSERT INTO doc_seq VALUES ('d', 1);
        """
    )
    connection.execute("PRAGMA user_version = 0")  # older code never stamped the version
    connection.commit()
    connection.close()
    reopened = SqliteStorage(path)
    with reopened.read() as tx:
        rows = tx.entries("d")
    assert [(e.id, e.valid_time, e.synthetic, e.purged) for e in rows] == [("x", None, None, False)]
    reopened.close()

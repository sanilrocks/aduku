"""T051: a SQLite database made before feature 002 opens, keeps its views, and takes impure outcomes."""

import sqlite3
from datetime import UTC, datetime

from aduku.model import EffectRecord
from aduku.storage.sqlite import SqliteStorage

OLD_SCHEMA = """
CREATE TABLE views (
    doc TEXT PRIMARY KEY, watermark_seq INTEGER NOT NULL, schema_version INTEGER NOT NULL,
    rules_fingerprint TEXT NOT NULL, document TEXT NOT NULL, model_errors TEXT NOT NULL, provenance TEXT NOT NULL
);
INSERT INTO views VALUES ('d1', 4, 1, 'fp', '{name:"Alice"}', '[]', '{conflicts:[],skipped:[]}');
"""


def old_database(path: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(OLD_SCHEMA)
    connection.close()


def test_a_database_from_before_the_effect_log_opens_and_its_view_reads_back(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = str(tmp_path / "old.db")
    old_database(path)
    storage = SqliteStorage(path)
    with storage.read() as tx:
        view = tx.get_view("d1")
    assert view is not None and (view.watermark_seq, view.document, view.effects_seq) == (4, '{name:"Alice"}', 0)
    storage.close()


def test_the_new_tables_work_in_a_migrated_database(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = str(tmp_path / "old.db")
    old_database(path)
    storage = SqliteStorage(path)
    record = EffectRecord(
        "d1", "/taxId", "fp", "fx.echo@1", "value", '"tax-US"', None, 1, datetime(2026, 10, 19, tzinfo=UTC)
    )
    with storage.transaction() as tx:
        assert tx.append_effects([record]) == [1]
        view = tx.get_view("d1")
        assert view is not None
        tx.put_view(type(view)(**{**view.__dict__, "effects_seq": 1}))
    with storage.read() as tx:
        stored = tx.get_view("d1")
        assert stored is not None and stored.effects_seq == 1 and tx.effects_seq("d1") == 1
    storage.close()


def test_opening_a_migrated_database_again_changes_nothing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = str(tmp_path / "old.db")
    old_database(path)
    for _ in range(3):
        storage = SqliteStorage(path)
        with storage.read() as tx:
            view = tx.get_view("d1")
        assert view is not None and view.document == '{name:"Alice"}'
        storage.close()
    connection = sqlite3.connect(path)
    columns = [row[1] for row in connection.execute("PRAGMA table_info(views)")]
    connection.close()
    assert columns.count("effects_seq") == 1


def test_an_old_database_still_protects_its_effect_log(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import pytest

    path = str(tmp_path / "old.db")
    old_database(path)
    storage = SqliteStorage(path)
    record = EffectRecord(
        "d1", "/taxId", "fp", "fx.echo@1", "value", '"tax-US"', None, 1, datetime(2026, 10, 19, tzinfo=UTC)
    )
    with storage.transaction() as tx:
        tx.append_effects([record])
    with pytest.raises(sqlite3.Error, match="append-only"):  # the triggers exist in a migrated database too
        storage.connection.execute("UPDATE effects SET value = 'x'")
    with pytest.raises(sqlite3.Error, match="append-only"):
        storage.connection.execute("DELETE FROM effects")
    storage.close()

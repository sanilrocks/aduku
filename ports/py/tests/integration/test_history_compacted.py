"""Feature 005 (FR-028, FR-029, research R-10, R-11): time travel and schema changes on compacted history."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from aduku.errors import HistoryCompactedError, SchemaError
from aduku.model import AsOf
from aduku.schema.loader import load_schema
from aduku.storage.base import Storage
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.service_helpers import ALICE, FIELDS, make_service, put, schema_text

V1 = schema_text(fields=FIELDS)
MIN_AGE = FIELDS.replace('"/age": {type: int, merge: max}', '"/age": {type: int, merge: min}')


def history(backend: Storage, valid: bool = False):  # type: ignore[no-untyped-def]
    db = make_service(backend, schema=V1)
    moment = datetime(2024, 3, 1, tzinfo=UTC)
    live = {}
    for i in range(1, 7):
        extra = {"valid_time": moment + timedelta(days=i)} if valid else {}
        result = db.append("d", [put("/name", f"n{i}", **extra), put("/age", (i * 7) % 5 + 1)], contributor=ALICE)
        live[result.entries[-1][1]] = result.view
    return db, live


def test_a_point_inside_a_compacted_range_is_answered_from_the_archive_while_it_is_kept(backend: Storage) -> None:
    db, live = history(backend, valid=True)
    entries = db.list_entries("d")
    db.compact("d", through_seq=8)
    for seq in (2, 4, 6, 8, 10, 12):
        assert db.get("d", as_of=AsOf(seq=seq)).document == live[seq].document  # type: ignore[union-attr]
    by_time = db.get("d", as_of=AsOf(tx_time=entries[4].tx_time + timedelta(milliseconds=1)))
    assert by_time.as_of.resolved_seq == 6 and by_time.document["name"] == "n3"  # both entries of that request  # type: ignore[union-attr]
    by_world = db.get("d", as_of=AsOf(valid_time=datetime(2024, 3, 3, 12, tzinfo=UTC)))
    assert by_world.document["name"] == "n2"


def test_after_expiry_only_points_that_can_be_answered_exactly_are(backend: Storage) -> None:
    db, live = history(backend, valid=True)
    entries = db.list_entries("d")
    db.compact("d", through_seq=8)
    db.expire_archive("d", through_seq=8)
    with pytest.raises(HistoryCompactedError) as caught:
        db.get("d", as_of=AsOf(seq=4))
    assert caught.value.earliest_seq == 8
    assert db.get("d", as_of=AsOf(seq=8)).document == live[8].document  # type: ignore[union-attr]
    assert db.get("d", as_of=AsOf(seq=12)).document == live[12].document  # type: ignore[union-attr]
    with pytest.raises(HistoryCompactedError):
        db.get(
            "d", as_of=AsOf(tx_time=entries[4].tx_time)
        )  # between the first and the last recording time of the range
    assert db.get("d", as_of=AsOf(tx_time=entries[0].tx_time - timedelta(days=1))).as_of.resolved_seq == 0  # type: ignore[union-attr]
    assert db.get("d", as_of=AsOf(tx_time=entries[-1].tx_time)).document == live[12].document  # type: ignore[union-attr]
    with pytest.raises(HistoryCompactedError):
        db.get("d", as_of=AsOf(valid_time=datetime(2024, 3, 3, tzinfo=UTC)))  # the range held entries with a valid_time
    with pytest.raises(HistoryCompactedError):
        db.explain("d", "/name", as_of=AsOf(seq=3))


def test_valid_time_reads_after_expiry_work_when_no_compacted_entry_stated_one(backend: Storage) -> None:
    db, live = history(backend, valid=False)
    db.compact("d", through_seq=8)
    db.expire_archive("d", through_seq=8)
    past = db.get("d", as_of=AsOf(valid_time=datetime(2020, 1, 1, tzinfo=UTC)))
    assert past.document == live[12].document  # type: ignore[union-attr]  # none stated a time: all of them always take part


def reference_under_v2(backend: Storage):  # type: ignore[no-untyped-def]
    from tests.snapshot_helpers import restart

    db, _ = history(backend)
    return db, restart


def test_a_version_that_folds_differently_is_derived_from_the_archive_while_it_is_kept(backend: Storage) -> None:
    db, _ = history(backend)
    db.compact("d", through_seq=8)
    v2 = load_schema(schema_text(version=2, fields=MIN_AGE), db.behaviors)
    db.schemas.put(v2)
    db.schemas.activate(2)  # allowed: the originals can be restored
    assert (
        db.get("d").document["age"] == 1
    )  # the minimum over all twelve entries, not over the compacted layers' maxima
    other = make_service(MemoryStorage(), schema=schema_text(fields=MIN_AGE))
    for i in range(1, 7):
        other.append("d", [put("/name", f"n{i}"), put("/age", (i * 7) % 5 + 1)], contributor=ALICE)
    assert db.get("d").document == other.get("d").document


def test_after_expiry_such_a_version_is_refused_on_activate_and_pin_and_read(backend: Storage) -> None:
    db, _ = history(backend)
    db.compact("d", through_seq=8)
    db.expire_archive("d", through_seq=8)
    v2 = load_schema(schema_text(version=2, fields=MIN_AGE), db.behaviors)
    db.schemas.put(v2)
    with pytest.raises(SchemaError) as caught:
        db.schemas.activate(2)
    assert "HISTORY_COMPACTED" in caught.value.codes
    with pytest.raises(SchemaError):
        db.schemas.pin("d", 2)
    db.schemas.activate(1)  # unchanged


def test_a_version_that_only_changes_validation_reuses_the_synthetic_entries(backend: Storage) -> None:
    db, _ = history(backend)
    db.compact("d", through_seq=8)
    db.expire_archive("d", through_seq=8)
    before = db.get("d").document
    v3 = load_schema(
        schema_text(
            version=3,
            fields=FIELDS.replace(
                '"/name": {type: string, merge: lww}',
                '"/name": {type: string, merge: lww, validate: [max_len::{n: 1}]}',
            ),
        ),
        db.behaviors,
    )
    db.schemas.put(v3)
    db.schemas.activate(3)
    after = db.get("d")
    assert after.document == before and after.model_errors  # "n6" is longer than one character: now a model error
    assert [e.path_scope for e in after.model_errors] == ["/name"]


def tamper_archive(backend: Storage) -> None:
    """Change an archived original that decides the document (the age is a maximum)."""
    if isinstance(backend, MemoryStorage):
        rows = backend._state.archive["d"]  # noqa: SLF001
        index = next(i for i, (_, entry) in enumerate(rows) if entry.path == "/age")
        rows[index] = (rows[index][0], replace(rows[index][1], value=99))
    else:
        assert isinstance(backend, SqliteStorage)
        connection = backend.connection
        connection.execute("UPDATE ledger_rewrite SET active = 1")
        connection.execute("UPDATE archive_entries SET value = '99' WHERE path = '/age' AND seq = 2")
        connection.execute("UPDATE ledger_rewrite SET active = 0")


def test_verify_archive_reports_identical_and_then_a_tampered_original(backend: Storage) -> None:
    db, _ = history(backend)
    db.compact("d", through_seq=8)
    assert db.verify_archive("d").checked and db.verify_archive("d").differences == ()
    tamper_archive(backend)
    report = db.verify_archive("d")
    assert report.checked and report.differences
